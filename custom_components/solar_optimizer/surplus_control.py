"""Deterministic surplus constraints and elapsed-time switching confirmation.

The optimizer chooses loads; this controller decides whether they can actually
run. It never controls the home battery or unmanaged household loads.
"""

from dataclasses import dataclass


@dataclass
class PendingDecision:
    direction: int
    target: float
    since: float


class SurplusController:
    """Pure controller: callers supply monotonic time and observed equipment."""

    def __init__(self, interval: float = 10):
        self.interval = interval
        self.pending: dict[str, PendingDecision] = {}
        self.commands: dict[str, tuple[float, float]] = {}
        self.deadline: float | None = None
        self.budget: float | None = None
        self.shortfall: float | None = None

    def record_command(self, name: str, power: float, now: float) -> None:
        self.commands[name] = (now, power)
        self.pending.pop(name, None)

    def command_pending(self, name: str, power: float, now: float) -> bool:
        command = self.commands.get(name)
        return bool(command and command[1] == power and now < command[0] + self.interval)

    @staticmethod
    def _shed(targets: dict, equipment: list[dict], budget: float) -> None:
        deficit = sum(targets.values()) - max(0, budget)
        for item in sorted(equipment, key=lambda e: (e["priority"], e["name"]), reverse=True):
            name = item["name"]
            while deficit > 0 and targets[name] > 0:
                old = targets[name]
                new = max(0, old - item["power_step"]) if item["can_change_power"] else 0
                if new < item["power_min"]:
                    new = 0
                targets[name] = new
                deficit -= old - new

    def decide(self, equipment: list[dict], proposed: list[dict], effective: float | None, now: float) -> list[dict]:
        """Return safe targets; an interval-long transient may retain old power.

        effective = grid + battery + charge reserve + export headroom.
        Equipment current_power must be observed, not a proposed allocation.
        """
        names = {e["name"] for e in equipment}
        self.pending = {k: v for k, v in self.pending.items() if k in names}
        self.commands = {k: v for k, v in self.commands.items() if k in names}
        self.deadline = None
        deadlines = []
        current = {e["name"]: e["current_power"] for e in equipment}
        total_current = sum(current.values())
        self.budget = None if effective is None else max(0, total_current - effective)
        budget = self.budget or 0
        commitments = dict(current)
        for name, (sent, power) in list(self.commands.items()):
            if current[name] == power and now >= sent + self.interval:
                del self.commands[name]
            else:
                commitments[name] = max(current[name], power)
                # Recheck even when the underlying device emits no state event.
                deadlines.append(max(now + max(self.interval, 1), sent + self.interval) if now >= sent + self.interval else sent + self.interval)

        choices = {e["name"]: e for e in proposed}
        targets = {}
        # Physical shortage takes precedence over stochastic load selection.
        shortage = effective is None or sum(commitments.values()) > budget
        for item in equipment:
            name = item["name"]
            choice = choices.get(name)
            target = commitments[name] if shortage else current[name] if choice is None else (choice["requested_power"] if choice["state"] else 0)
            pending = self.pending.get(name)
            if not shortage and pending and pending.direction > 0:
                # Keep confirming the selected load while physics permits it;
                # random optimizer proposals must not restart a stable-surplus clock.
                target = max(target, pending.target)
            targets[name] = 0 if item.get("stop_reason") or effective is None else target
        self._shed(targets, equipment, budget)
        for item in equipment:
            # Respect a variable device's setpoint interval by stopping instead of
            # sending an early power adjustment when a confirmed shortage requires it.
            name = item["name"]
            if item.get("increase_waiting") and 0 < targets[name] < current[name]:
                targets[name] = 0

        # Do not admit loads using power from unconfirmed/undelivered reductions.
        headroom = budget - sum(commitments.values())
        result = []
        for item in sorted(equipment, key=lambda e: (e["priority"], e["name"])):
            item = dict(item)
            name = item["name"]
            # A start in flight is an occupied load until explicitly cancelled,
            # even if the switch has not yet acknowledged it.
            old = commitments[name]
            target = targets[name]
            reason = item.get("stop_reason")
            deadline = None
            if reason:
                self.pending.pop(name, None)
                target = 0
            else:
                direction = (target > old) - (target < old)
                if direction > 0 and (item.get("increase_waiting") or target - old > headroom):
                    self.pending.pop(name, None)
                    target = old
                    reason = "minimum_off_or_power_interval" if item.get("increase_waiting") else "insufficient_surplus"
                elif direction:
                    pending = self.pending.get(name)
                    if pending is None or pending.direction != direction or (direction > 0 and target > pending.target):
                        pending = PendingDecision(direction, target, now)
                        self.pending[name] = pending
                    pending.target = target
                    deadline = pending.since + self.interval
                    if name in self.commands:
                        deadline = max(deadline, self.commands[name][0] + self.interval)
                    reason = "invalid_sensor" if effective is None else ("waiting_for_stable_surplus" if direction > 0 else "insufficient_surplus")
                    if direction > 0:
                        headroom -= target - old
                    if now < deadline:
                        deadlines.append(deadline)
                        target = old
                    # Keep an expired confirmation until observation/command acknowledgement;
                    # repeated calculations must not restart the confirmation clock.
                else:
                    self.pending.pop(name, None)
                    reason = "invalid_sensor" if effective is None else ("running_on_surplus" if old else "idle")
            item.update(state=target > 0, requested_power=target, decision_reason=reason, pending_deadline=deadline)
            result.append(item)
        if deadlines:
            self.deadline = min(deadlines)
        # Signed household deficit can remain after every optional load is off.
        self.shortfall = None if effective is None else max(0, effective + sum(e["requested_power"] for e in result) - total_current)
        return result

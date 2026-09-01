"""Tests for battery-aware surplus policies."""

from homeassistant.core import HomeAssistant

from custom_components.solar_optimizer.const import (
    BATTERY_POWER_STRATEGY_CHARGE_FIRST,
    BATTERY_POWER_STRATEGY_CHARGE_FIRST_WITH_BUDGET,
    BATTERY_POWER_STRATEGY_EXISTING,
)
from custom_components.solar_optimizer.coordinator import SolarOptimizerCoordinator


def create_coordinator(
    hass: HomeAssistant,
    strategy: str,
    minimum_export_power: float = 0,
    maximum_battery_charge_reserve_power: float = 100,
) -> SolarOptimizerCoordinator:
    """Create a coordinator configured for a battery policy test."""
    coordinator = SolarOptimizerCoordinator(hass, None)
    coordinator._battery_power_strategy = strategy
    coordinator._minimum_export_power = minimum_export_power
    coordinator._battery_budget_start_soc = 100
    coordinator._battery_budget_stop_soc = 90
    coordinator._battery_budget_active = False
    coordinator._maximum_battery_charge_reserve_power = (
        maximum_battery_charge_reserve_power
    )
    coordinator._battery_charge_reserve_start_soc = 50
    return coordinator


async def test_existing_strategy_preserves_current_behavior(
    hass: HomeAssistant,
) -> None:
    """The default policy must continue adding signed battery power."""
    coordinator = create_coordinator(hass, BATTERY_POWER_STRATEGY_EXISTING)

    assert coordinator._effective_power_consumption(0, -500) == -500
    assert coordinator._effective_power_consumption(0, 500) == 500
    assert coordinator._effective_power_consumption(-1000, -500) == -1500


async def test_charge_first_reserves_charging_and_export_margin(
    hass: HomeAssistant,
) -> None:
    """Charging is not surplus, discharge is a deficit, and margin is reserved."""
    coordinator = create_coordinator(
        hass,
        BATTERY_POWER_STRATEGY_CHARGE_FIRST,
        minimum_export_power=200,
    )

    assert coordinator._effective_power_consumption(-1500, -500) == -1700
    assert coordinator._effective_power_consumption(0, -500) == -200
    assert coordinator._effective_power_consumption(0, -300) == 0
    assert coordinator._effective_power_consumption(0, -250) == 50
    assert coordinator._effective_power_consumption(0, 500) == 800
    assert coordinator._effective_power_consumption(None, -500) is None


async def test_battery_budget_uses_soc_hysteresis(hass: HomeAssistant) -> None:
    """The budget opens at 100%, stays open, and closes at 90%."""
    coordinator = create_coordinator(
        hass,
        BATTERY_POWER_STRATEGY_CHARGE_FIRST_WITH_BUDGET,
        minimum_export_power=200,
    )

    coordinator._update_battery_budget(99)
    assert coordinator._battery_budget_active is False
    assert coordinator._effective_power_consumption(0, -500) == -200

    coordinator._update_battery_budget(100)
    assert coordinator._battery_budget_active is True
    assert coordinator._effective_power_consumption(0, 500) == 0

    coordinator._update_battery_budget(95)
    assert coordinator._battery_budget_active is True
    assert coordinator._effective_power_consumption(0, 500) == 0

    coordinator._update_battery_budget(90)
    assert coordinator._battery_budget_active is False
    assert coordinator._effective_power_consumption(0, 500) == 800


async def test_soc_taper_calculates_2700_watt_reserve_curve(
    hass: HomeAssistant,
) -> None:
    """The reserve tapers from the configured start SOC to the open SOC."""
    coordinator = create_coordinator(
        hass,
        BATTERY_POWER_STRATEGY_CHARGE_FIRST_WITH_BUDGET,
        maximum_battery_charge_reserve_power=2700,
    )
    coordinator._battery_charge_reserve_start_soc = 50

    expected_reserves = {
        49: 2700,
        50: 2700,
        59.9: 2700,
        60: 2160,
        70: 1620,
        80: 1080,
        90: 540,
        94.9: 540,
        95: 270,
        99.9: 270,
        100: 0,
    }
    for soc, expected in expected_reserves.items():
        assert coordinator._effective_battery_charge_reserve_power(soc) == expected


async def test_soc_taper_reserve_is_a_floor_not_a_charging_cap(
    hass: HomeAssistant,
) -> None:
    """Charging above the reserve remains usable while the floor is preserved."""
    coordinator = create_coordinator(
        hass,
        BATTERY_POWER_STRATEGY_CHARGE_FIRST_WITH_BUDGET,
        maximum_battery_charge_reserve_power=2700,
    )
    coordinator._battery_charge_reserve_start_soc = 50

    # At 90% the floor is 540 W. Charging at 1100 W leaves 560 W usable.
    assert coordinator._effective_power_consumption(0, -1100, 90) == -560
    # At 50% the full 2700 W reserve remains in force.
    assert coordinator._effective_power_consumption(0, -1100, 50) == 1600


async def test_non_budget_strategy_clears_latch(hass: HomeAssistant) -> None:
    """Changing away from the budget policy cannot leave discharge enabled."""
    coordinator = create_coordinator(hass, BATTERY_POWER_STRATEGY_CHARGE_FIRST_WITH_BUDGET)
    coordinator._update_battery_budget(100)
    assert coordinator._battery_budget_active is True

    coordinator._battery_power_strategy = BATTERY_POWER_STRATEGY_CHARGE_FIRST
    coordinator._update_battery_budget(100)
    assert coordinator._battery_budget_active is False


async def test_hard_constraint_stops_waiting_on_off_device(
    hass: HomeAssistant,
) -> None:
    """The charging floor overrides price, priority, and minimum-on waiting."""
    coordinator = create_coordinator(hass, BATTERY_POWER_STRATEGY_CHARGE_FIRST)
    solution = [
        {
            "name": "Pool Pump",
            "state": True,
            "is_waiting": True,
            "current_power": 1100,
            "requested_power": 1100,
            "can_change_power": False,
            "power_min": 1100,
            "power_step": 1100,
            "priority": 1,
        }
    ]

    constrained, total_power = coordinator._enforce_minimum_charge_constraint(
        solution, effective_power_consumption=290
    )

    assert constrained[0]["state"] is False
    assert constrained[0]["requested_power"] == 0
    assert total_power == 0


async def test_hard_constraint_reduces_variable_power_in_steps(
    hass: HomeAssistant,
) -> None:
    """A variable load is reduced before it is stopped."""
    coordinator = create_coordinator(hass, BATTERY_POWER_STRATEGY_CHARGE_FIRST)
    solution = [
        {
            "name": "Variable Load",
            "state": True,
            "is_waiting": True,
            "current_power": 1000,
            "requested_power": 1000,
            "can_change_power": True,
            "power_min": 100,
            "power_step": 100,
            "priority": 1,
        }
    ]

    constrained, total_power = coordinator._enforce_minimum_charge_constraint(
        solution, effective_power_consumption=250
    )

    assert constrained[0]["state"] is True
    assert constrained[0]["requested_power"] == 700
    assert total_power == 700


async def test_hard_constraint_prevents_start_without_enough_charge(
    hass: HomeAssistant,
) -> None:
    """An off device cannot start if it would cross the charging floor."""
    coordinator = create_coordinator(hass, BATTERY_POWER_STRATEGY_CHARGE_FIRST)
    solution = [
        {
            "name": "Pool Pump",
            "state": True,
            "is_waiting": False,
            "current_power": 0,
            "requested_power": 1100,
            "can_change_power": False,
            "power_min": 1100,
            "power_step": 1100,
            "priority": 1,
        }
    ]

    constrained, total_power = coordinator._enforce_minimum_charge_constraint(
        solution, effective_power_consumption=-500
    )

    assert constrained[0]["state"] is False
    assert constrained[0]["requested_power"] == 0
    assert total_power == 0


async def test_open_budget_bypasses_charging_floor(hass: HomeAssistant) -> None:
    """The configured SOC budget still permits deliberate battery use."""
    coordinator = create_coordinator(
        hass, BATTERY_POWER_STRATEGY_CHARGE_FIRST_WITH_BUDGET
    )
    coordinator._battery_budget_active = True
    solution = [
        {
            "state": True,
            "current_power": 1100,
            "requested_power": 1100,
            "can_change_power": False,
            "power_min": 1100,
            "power_step": 1100,
            "priority": 1,
        }
    ]

    constrained, total_power = coordinator._enforce_minimum_charge_constraint(
        solution, effective_power_consumption=290
    )

    assert constrained[0]["state"] is True
    assert total_power == 1100


def fixed_load_solution(state: bool, requested_power: float) -> list[dict]:
    """Return a fixed-load optimizer solution for reversal-hold tests."""
    return [
        {
            "name": "Pool Pump",
            "state": state,
            "current_power": 1100 if state else 0,
            "requested_power": requested_power,
        }
    ]


async def test_reversal_hold_does_not_delay_first_decision(
    hass: HomeAssistant,
) -> None:
    """Without a previous command, an initial on or off decision is immediate."""
    coordinator = create_coordinator(hass, BATTERY_POWER_STRATEGY_CHARGE_FIRST)
    coordinator._decision_reversal_hold_sec = 10
    solution = fixed_load_solution(False, 0)

    held, total_power = coordinator._apply_decision_reversal_hold(solution, now=105)

    assert held[0]["state"] is False
    assert "decision_reversal_held" not in held[0]
    assert total_power == 0


async def test_reversal_hold_suppresses_off_after_on(
    hass: HomeAssistant,
) -> None:
    """A transient deficit cannot reverse a recent activation."""
    coordinator = create_coordinator(hass, BATTERY_POWER_STRATEGY_CHARGE_FIRST)
    coordinator._decision_reversal_hold_sec = 10
    coordinator._last_state_change_command["Pool Pump"] = (100, True, 1100)
    solution = fixed_load_solution(False, 0)

    held, total_power = coordinator._apply_decision_reversal_hold(solution, now=105)

    assert held[0]["state"] is True
    assert held[0]["requested_power"] == 1100
    assert held[0]["decision_reversal_held"] is True
    assert total_power == 1100


async def test_reversal_hold_suppresses_on_after_off(
    hass: HomeAssistant,
) -> None:
    """Surplus returning immediately cannot reverse a recent deactivation."""
    coordinator = create_coordinator(hass, BATTERY_POWER_STRATEGY_CHARGE_FIRST)
    coordinator._decision_reversal_hold_sec = 10
    coordinator._last_state_change_command["Pool Pump"] = (100, False, 0)
    solution = fixed_load_solution(True, 1100)

    held, total_power = coordinator._apply_decision_reversal_hold(solution, now=109.9)

    assert held[0]["state"] is False
    assert held[0]["requested_power"] == 0
    assert held[0]["decision_reversal_held"] is True
    assert total_power == 0


async def test_reversal_is_allowed_at_hold_expiry(hass: HomeAssistant) -> None:
    """The opposite decision is allowed as soon as the hold reaches its limit."""
    coordinator = create_coordinator(hass, BATTERY_POWER_STRATEGY_CHARGE_FIRST)
    coordinator._decision_reversal_hold_sec = 10
    coordinator._last_state_change_command["Pool Pump"] = (100, True, 1100)
    solution = fixed_load_solution(False, 0)

    held, total_power = coordinator._apply_decision_reversal_hold(solution, now=110)

    assert held[0]["state"] is False
    assert "decision_reversal_held" not in held[0]
    assert total_power == 0


async def test_same_command_is_not_repeated_while_state_settles(
    hass: HomeAssistant,
) -> None:
    """A lagging switch state cannot reset the hold timer on every event."""
    coordinator = create_coordinator(hass, BATTERY_POWER_STRATEGY_CHARGE_FIRST)
    coordinator._decision_reversal_hold_sec = 10
    coordinator._last_state_change_command["Pool Pump"] = (100, True, 1100)

    assert coordinator._is_recent_state_change_command(
        "Pool Pump", True, now=105
    )
    assert not coordinator._is_recent_state_change_command(
        "Pool Pump", False, now=105
    )
    assert not coordinator._is_recent_state_change_command(
        "Pool Pump", True, now=110
    )

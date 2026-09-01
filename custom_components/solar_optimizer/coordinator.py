""" The data coordinator class """

import logging
import math
import time as monotonic_time
from datetime import datetime, timedelta, time
from typing import Any

from homeassistant.core import HomeAssistant, Event, EventStateChangedData
from homeassistant.components.select import SelectEntity

from homeassistant.helpers.event import (
    async_track_state_change_event,
)

from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
)

from homeassistant.util.unit_conversion import (
    BaseUnitConverter,
    PowerConverter
)

from homeassistant.config_entries import ConfigEntry

from .const import (
    BATTERY_POWER_STRATEGY_CHARGE_FIRST_WITH_BUDGET,
    BATTERY_POWER_STRATEGY_EXISTING,
    CONF_BATTERY_BUDGET_START_SOC,
    CONF_BATTERY_BUDGET_STOP_SOC,
    CONF_BATTERY_CHARGE_RESERVE_START_SOC,
    CONF_BATTERY_POWER_STRATEGY,
    CONF_DECISION_REVERSAL_HOLD_SEC,
    CONF_MAXIMUM_BATTERY_CHARGE_RESERVE_POWER,
    CONF_MINIMUM_EXPORT_POWER,
    DEFAULT_BATTERY_BUDGET_START_SOC,
    DEFAULT_BATTERY_BUDGET_STOP_SOC,
    DEFAULT_DECISION_REVERSAL_HOLD_SEC,
    DEFAULT_BATTERY_CHARGE_RESERVE_START_SOC,
    DEFAULT_MAXIMUM_BATTERY_CHARGE_RESERVE_POWER,
    DEFAULT_MINIMUM_EXPORT_POWER,
    DEFAULT_RAZ_TIME,
    DEFAULT_REFRESH_PERIOD_SEC,
    SOLAR_OPTIMIZER_DOMAIN,
    battery_charge_reserve_power,
    name_to_unique_id,
)
from .managed_device import ManagedDevice
from .simulated_annealing_algo import SimulatedAnnealingAlgorithm

_LOGGER = logging.getLogger(__name__)


def get_safe_float(hass, entity_id: str, unit: str = None):
    """Get a safe float state value for an entity.
    Return None if entity is not available"""
    if entity_id is None or not (state := hass.states.get(entity_id)) or state.state == "unknown" or state.state == "unavailable":
        return None

    float_val = float(state.state)

    if (unit is not None) and ('device_class' in state.attributes) and (state.attributes["device_class"] == "power"):
        float_val = PowerConverter.convert(float_val,
            state.attributes["unit_of_measurement"],
            unit
        )

    return None if math.isinf(float_val) or not math.isfinite(float_val) else float_val


class SolarOptimizerCoordinator(DataUpdateCoordinator):
    """The coordinator class which is used to coordinate all update"""

    hass: HomeAssistant

    def __init__(self, hass: HomeAssistant, config):
        """Initialize the coordinator"""
        SolarOptimizerCoordinator.hass = hass
        self._devices: list[ManagedDevice] = []
        self._power_consumption_entity_id: str = None
        self._power_production_entity_id: str = None
        self._subscribe_to_events: bool = False
        self._unsub_events = None
        self._sell_cost_entity_id: str = None
        self._buy_cost_entity_id: str = None
        self._sell_tax_percent_entity_id: str = None
        self._smooth_production: bool = True
        self._last_production: float = 0.0
        self._battery_soc_entity_id: str = None
        self._battery_charge_power_entity_id: str = None
        self._battery_power_strategy: str = BATTERY_POWER_STRATEGY_EXISTING
        self._battery_budget_start_soc: float = DEFAULT_BATTERY_BUDGET_START_SOC
        self._battery_budget_stop_soc: float = DEFAULT_BATTERY_BUDGET_STOP_SOC
        self._maximum_battery_charge_reserve_power: float = (
            DEFAULT_MAXIMUM_BATTERY_CHARGE_RESERVE_POWER
        )
        self._battery_charge_reserve_start_soc: float = (
            DEFAULT_BATTERY_CHARGE_RESERVE_START_SOC
        )
        self._minimum_export_power: float = DEFAULT_MINIMUM_EXPORT_POWER
        self._decision_reversal_hold_sec: float = DEFAULT_DECISION_REVERSAL_HOLD_SEC
        self._last_state_change_command: dict[str, tuple[float, bool, float]] = {}
        self._battery_budget_active: bool = False
        self._raz_time: time = None

        self._central_config_done = False
        self._priority_weight_entity = None

        super().__init__(hass, _LOGGER, name="Solar Optimizer")

        init_temp = 1000
        min_temp = 0.05
        cooling_factor = 0.95
        max_iteration_number = 1000

        if config and (algo_config := config.get("algorithm")):
            init_temp = float(algo_config.get("initial_temp", 1000))
            min_temp = float(algo_config.get("min_temp", 0.05))
            cooling_factor = float(algo_config.get("cooling_factor", 0.95))
            max_iteration_number = int(algo_config.get("max_iteration_number", 1000))

        self._algo = SimulatedAnnealingAlgorithm(
            init_temp, min_temp, cooling_factor, max_iteration_number
        )
        self.config = config

    async def configure(self, config: ConfigEntry) -> None:
        """Configure the coordinator from configEntry of the integration"""
        refresh_period_sec = (
            config.data.get("refresh_period_sec") or DEFAULT_REFRESH_PERIOD_SEC
        )
        self.update_interval = timedelta(seconds=refresh_period_sec)
        self._schedule_refresh()

        self._power_consumption_entity_id = config.data.get(
            "power_consumption_entity_id"
        )
        self._power_production_entity_id = config.data.get("power_production_entity_id")
        self._subscribe_to_events = config.data.get("subscribe_to_events")

        if self._unsub_events is not None:
            self._unsub_events()
            self._unsub_events = None

        if self._subscribe_to_events:
            tracked_entities = [
                self._power_consumption_entity_id,
                self._power_production_entity_id,
                config.data.get("battery_soc_entity_id"),
                config.data.get("battery_charge_power_entity_id"),
            ]
            self._unsub_events = async_track_state_change_event(
                self.hass,
                [entity_id for entity_id in tracked_entities if entity_id],
                self._async_on_change)

        self._sell_cost_entity_id = config.data.get("sell_cost_entity_id")
        self._buy_cost_entity_id = config.data.get("buy_cost_entity_id")
        self._sell_tax_percent_entity_id = config.data.get("sell_tax_percent_entity_id")
        self._battery_soc_entity_id = config.data.get("battery_soc_entity_id")
        self._battery_charge_power_entity_id = config.data.get(
            "battery_charge_power_entity_id"
        )
        self._battery_power_strategy = config.data.get(
            CONF_BATTERY_POWER_STRATEGY, BATTERY_POWER_STRATEGY_EXISTING
        )
        self._battery_budget_start_soc = float(
            config.data.get(
                CONF_BATTERY_BUDGET_START_SOC,
                DEFAULT_BATTERY_BUDGET_START_SOC,
            )
        )
        self._battery_budget_stop_soc = float(
            config.data.get(
                CONF_BATTERY_BUDGET_STOP_SOC,
                DEFAULT_BATTERY_BUDGET_STOP_SOC,
            )
        )
        self._maximum_battery_charge_reserve_power = float(
            config.data.get(
                CONF_MAXIMUM_BATTERY_CHARGE_RESERVE_POWER,
                DEFAULT_MAXIMUM_BATTERY_CHARGE_RESERVE_POWER,
            )
        )
        self._battery_charge_reserve_start_soc = float(
            config.data.get(
                CONF_BATTERY_CHARGE_RESERVE_START_SOC,
                DEFAULT_BATTERY_CHARGE_RESERVE_START_SOC,
            )
        )
        self._minimum_export_power = float(
            config.data.get(
                CONF_MINIMUM_EXPORT_POWER,
                DEFAULT_MINIMUM_EXPORT_POWER,
            )
        )
        self._decision_reversal_hold_sec = float(
            config.data.get(
                CONF_DECISION_REVERSAL_HOLD_SEC,
                DEFAULT_DECISION_REVERSAL_HOLD_SEC,
            )
        )
        self._last_state_change_command.clear()
        # If Home Assistant restarts while the SOC is between the thresholds, start
        # conservatively. The budget opens again only after the upper threshold is met.
        self._battery_budget_active = False
        self._smooth_production = config.data.get("smooth_production") is True
        self._last_production = 0.0

        self._raz_time = datetime.strptime(
            config.data.get("raz_time") or DEFAULT_RAZ_TIME, "%H:%M"
        ).time()
        self._central_config_done = True

    async def on_ha_started(self, _) -> None:
        """Listen the homeassistant_started event to initialize the first calculation"""
        _LOGGER.info("First initialization of Solar Optimizer")

    async def _async_on_change(self, event: Event[EventStateChangedData]) -> None:
        await self.async_refresh()
        self._schedule_refresh()

    async def _async_update_data(self):
        _LOGGER.info("Refreshing Solar Optimizer calculation")

        calculated_data = {}

        # Check forced activation timers — stop and re-enable any device whose timer has expired
        for device in self._devices:
            expired = await device.expire_forced_activation()
            if expired:
                _LOGGER.info("Forced activation expired for %s — SO management re-enabled", device.name)

        # Add a device state attributes
        for _, device in enumerate(self._devices):
            # Initialize current power depending or reality
            device.set_current_power_with_device_state()

        # Add a power_consumption and power_production
        power_production = get_safe_float(self.hass, self._power_production_entity_id, "W")
        if power_production is None:
            _LOGGER.warning(
                "Power production is not valued. Solar Optimizer will be disabled"
            )
            return None

        if not self._smooth_production:
            calculated_data["power_production"] = power_production
        else:
            self._last_production = round(
                0.5 * self._last_production + 0.5 * power_production
            )
            calculated_data["power_production"] = self._last_production

        calculated_data["power_production_brut"] = power_production

        calculated_data["power_consumption"] = get_safe_float(
            self.hass, self._power_consumption_entity_id, "W"
        )

        calculated_data["sell_cost"] = get_safe_float(
            self.hass, self._sell_cost_entity_id
        )

        calculated_data["buy_cost"] = get_safe_float(
            self.hass, self._buy_cost_entity_id
        )

        calculated_data["sell_tax_percent"] = get_safe_float(
            self.hass, self._sell_tax_percent_entity_id
        )

        soc = get_safe_float(self.hass, self._battery_soc_entity_id)
        calculated_data["battery_soc"] = soc if soc is not None else 0

        charge_power = get_safe_float(self.hass, self._battery_charge_power_entity_id)
        calculated_data["battery_charge_power"] = (
            charge_power if charge_power is not None else 0
        )

        self._update_battery_budget(soc)
        calculated_data["battery_budget_active"] = self._battery_budget_active
        calculated_data["battery_power_strategy"] = self._battery_power_strategy
        calculated_data["maximum_battery_charge_reserve_power"] = (
            self._maximum_battery_charge_reserve_power
        )
        calculated_data["battery_charge_reserve_start_soc"] = (
            self._battery_charge_reserve_start_soc
        )
        calculated_data["effective_battery_charge_reserve_power"] = (
            self._effective_battery_charge_reserve_power(soc)
        )
        calculated_data["minimum_export_power"] = self._minimum_export_power
        calculated_data["decision_reversal_hold_sec"] = (
            self._decision_reversal_hold_sec
        )
        calculated_data["minimum_charge_constraint_active"] = (
            self._minimum_charge_constraint_active
        )
        calculated_data["effective_power_consumption"] = (
            self._effective_power_consumption(
                calculated_data["power_consumption"],
                calculated_data["battery_charge_power"],
                soc,
            )
        )
        calculated_data["usable_excess_power"] = (
            max(0, -calculated_data["effective_power_consumption"])
            if calculated_data["effective_power_consumption"] is not None
            else None
        )

        calculated_data["priority_weight"] = self.priority_weight

        #
        # Call Algorithm Recuit simulé
        #
        best_solution, best_objective, total_power = self._algo.recuit_simule(
            self._devices,
            calculated_data["effective_power_consumption"],
            calculated_data["power_production"],
            calculated_data["sell_cost"],
            calculated_data["buy_cost"],
            calculated_data["sell_tax_percent"],
            calculated_data["battery_soc"],
            calculated_data["priority_weight"],
        )

        best_solution, total_power = self._enforce_minimum_charge_constraint(
            best_solution,
            calculated_data["effective_power_consumption"],
        )
        best_solution, total_power = self._apply_decision_reversal_hold(best_solution)

        calculated_data["best_solution"] = best_solution
        calculated_data["best_objective"] = best_objective
        calculated_data["total_power"] = total_power

        # Uses the result to turn on or off or change power
        should_log = False
        for _, equipement in enumerate(best_solution):
            name = equipement["name"]
            requested_power = equipement.get("requested_power")
            state = equipement["state"]
            _LOGGER.debug("Dealing with best_solution for %s - %s", name, equipement)
            device = self.get_device_by_name(name)
            if not device:
                continue

            old_requested_power = device.requested_power
            is_active = device.is_active
            state_command_pending = False
            should_force_offpeak = device.should_be_forced_offpeak
            if calculated_data["minimum_charge_constraint_active"] and not state:
                should_force_offpeak = False
            if should_force_offpeak and self._is_recent_state_change_command(
                name, False
            ):
                should_force_offpeak = False
            if should_force_offpeak:
                _LOGGER.debug("%s - we should force %s name", self, name)
            if is_active and not state and not should_force_offpeak:
                if not self._is_recent_state_change_command(name, False):
                    _LOGGER.debug("Extinction de %s", name)
                    should_log = True
                    old_requested_power = 0
                    await device.deactivate()
                    self._record_state_change_command(name, False, 0)
            elif not is_active and (state or should_force_offpeak):
                state_command_pending = self._is_recent_state_change_command(
                    name, True
                )
                if not state_command_pending:
                    _LOGGER.debug("Allumage de %s", name)
                    should_log = True
                    old_requested_power = requested_power
                    await device.activate(requested_power)
                    self._record_state_change_command(
                        name, True, requested_power or device.power_max
                    )

            # Send change power if state is now on and change power is accepted and (power have change or eqt is just activated)
            if (
                state
                and not state_command_pending
                and device.can_change_power
                and (device.current_power != requested_power or not is_active)
            ):
                _LOGGER.debug(
                    "Change power of %s to %s",
                    equipement["name"],
                    requested_power,
                )
                should_log = True
                await device.change_requested_power(requested_power)

            device.set_requested_power(old_requested_power)

            # Add updated data to the result
            calculated_data[name_to_unique_id(name)] = device

        if should_log:
            _LOGGER.info("Calculated data are: %s", calculated_data)
        else:
            _LOGGER.debug("Calculated data are: %s", calculated_data)

        return calculated_data

    def _update_battery_budget(self, battery_soc: float | None) -> None:
        """Update the battery budget latch using separate start and stop thresholds."""
        if (
            self._battery_power_strategy
            != BATTERY_POWER_STRATEGY_CHARGE_FIRST_WITH_BUDGET
            or battery_soc is None
        ):
            self._battery_budget_active = False
            return

        if battery_soc >= self._battery_budget_start_soc:
            self._battery_budget_active = True
        elif battery_soc <= self._battery_budget_stop_soc:
            self._battery_budget_active = False

    def _effective_battery_charge_reserve_power(
        self, battery_soc: float | None
    ) -> float:
        """Return the SOC-tapered charging reserve for this cycle."""
        return battery_charge_reserve_power(
            self._maximum_battery_charge_reserve_power,
            self._battery_charge_reserve_start_soc,
            self._battery_budget_stop_soc,
            self._battery_budget_start_soc,
            battery_soc,
        )

    def _effective_power_consumption(
        self,
        grid_power: float | None,
        battery_power: float,
        battery_soc: float | None = None,
    ) -> float | None:
        """Return net power presented to the optimizer for the selected policy.

        Grid power is negative while exporting. Battery power is negative while
        charging and positive while discharging.
        """
        if grid_power is None:
            return None

        if self._battery_power_strategy == BATTERY_POWER_STRATEGY_EXISTING:
            return grid_power + battery_power

        if (
            self._battery_power_strategy
            == BATTERY_POWER_STRATEGY_CHARGE_FIRST_WITH_BUDGET
            and self._battery_budget_active
        ):
            # Once opened at the upper SOC threshold, let an already-running load
            # ride through solar dips using the battery until the lower threshold.
            return grid_power

        # Flexible loads may reduce charging, but the configured charging floor and
        # export margin are reserved. A positive value means the floor is violated.
        return (
            grid_power
            + battery_power
            + self._effective_battery_charge_reserve_power(battery_soc)
            + self._minimum_export_power
        )

    @property
    def _minimum_charge_constraint_active(self) -> bool:
        """Return whether the hard charging floor applies in the current cycle."""
        return self._battery_power_strategy != BATTERY_POWER_STRATEGY_EXISTING and not (
            self._battery_power_strategy
            == BATTERY_POWER_STRATEGY_CHARGE_FIRST_WITH_BUDGET
            and self._battery_budget_active
        )

    def _enforce_minimum_charge_constraint(
        self,
        solution: list[dict],
        effective_power_consumption: float | None,
    ) -> tuple[list[dict], float]:
        """Shed flexible power until the battery charging floor is respected.

        This final deterministic pass makes the floor independent of energy prices,
        priorities, and normal minimum-on timers. Lower-priority devices are shed
        first; variable-power devices are reduced in configured steps before stopping.
        """
        total_power = self._algo.consommation_equipements(solution)
        if (
            not self._minimum_charge_constraint_active
            or effective_power_consumption is None
            or not solution
        ):
            return solution, total_power

        current_power = sum(
            equipment["current_power"]
            for equipment in solution
            if equipment["state"] or equipment["current_power"] > 0
        )
        projected_deficit = effective_power_consumption + total_power - current_power
        if projected_deficit <= 0:
            return solution, total_power

        for equipment in sorted(
            (equipment for equipment in solution if equipment["state"]),
            key=lambda equipment: equipment["priority"],
            reverse=True,
        ):
            requested_power = equipment["requested_power"]
            if equipment["can_change_power"]:
                while requested_power > 0 and projected_deficit > 0:
                    new_power = max(0, requested_power - equipment["power_step"])
                    if 0 < new_power < equipment["power_min"]:
                        new_power = 0
                    projected_deficit -= requested_power - new_power
                    requested_power = new_power
                equipment["requested_power"] = requested_power
                equipment["state"] = requested_power > 0
            else:
                equipment["state"] = False
                equipment["requested_power"] = 0
                projected_deficit -= requested_power

            if projected_deficit <= 0:
                break

        return solution, self._algo.consommation_equipements(solution)

    def _record_state_change_command(
        self, device_name: str, state: bool, requested_power: float
    ) -> None:
        """Record an optimizer on/off command for reversal throttling."""
        self._last_state_change_command[device_name] = (
            monotonic_time.monotonic(),
            state,
            requested_power,
        )

    def _is_recent_state_change_command(
        self,
        device_name: str,
        state: bool,
        now: float | None = None,
    ) -> bool:
        """Return whether this same command is already settling."""
        previous = self._last_state_change_command.get(device_name)
        if previous is None or self._decision_reversal_hold_sec <= 0:
            return False

        changed_at, commanded_state, _ = previous
        current_time = monotonic_time.monotonic() if now is None else now
        return (
            commanded_state == state
            and current_time - changed_at < self._decision_reversal_hold_sec
        )

    def _apply_decision_reversal_hold(
        self,
        solution: list[dict],
        now: float | None = None,
    ) -> tuple[list[dict], float]:
        """Suppress only the opposite state decision during the settling window.

        With no prior command, the first decision is immediate. Repeating the same
        decision is allowed; only a command that would reverse the last optimizer
        state change is held until the configured number of seconds has elapsed.
        """
        if self._decision_reversal_hold_sec <= 0:
            return solution, self._algo.consommation_equipements(solution)

        current_time = monotonic_time.monotonic() if now is None else now
        for equipment in solution:
            previous = self._last_state_change_command.get(equipment["name"])
            if previous is None:
                continue

            changed_at, commanded_state, commanded_power = previous
            if (
                equipment["state"] != commanded_state
                and current_time - changed_at < self._decision_reversal_hold_sec
            ):
                equipment["state"] = commanded_state
                equipment["requested_power"] = (
                    commanded_power if commanded_state else 0
                )
                equipment["decision_reversal_held"] = True

        return solution, self._algo.consommation_equipements(solution)

    @classmethod
    def get_coordinator(cls) -> Any:
        """Get the coordinator from the hass.data"""
        if (
            not hasattr(SolarOptimizerCoordinator, "hass")
            or SolarOptimizerCoordinator.hass is None
            or SolarOptimizerCoordinator.hass.data.get(SOLAR_OPTIMIZER_DOMAIN) is None
        ):
            return None

        return SolarOptimizerCoordinator.hass.data[SOLAR_OPTIMIZER_DOMAIN][
            "coordinator"
        ]

    @classmethod
    def reset(cls) -> Any:
        """Reset the coordinator from the hass.data"""
        if (
            not hasattr(SolarOptimizerCoordinator, "hass")
            or SolarOptimizerCoordinator.hass is None
            or SolarOptimizerCoordinator.hass.data.get(SOLAR_OPTIMIZER_DOMAIN) is None
        ):
            return

        SolarOptimizerCoordinator.hass.data[SOLAR_OPTIMIZER_DOMAIN][
            "coordinator"
        ] = None

    @property
    def is_central_config_done(self) -> bool:
        """Return True if the central config is done"""
        return self._central_config_done

    @property
    def devices(self) -> list[ManagedDevice]:
        """Get all the managed device"""
        return self._devices

    def get_device_by_name(self, name: str) -> ManagedDevice | None:
        """Returns the device which name is given in argument"""
        for _, device in enumerate(self._devices):
            if device.name == name:
                return device
        return None

    def get_device_by_unique_id(self, uid: str) -> ManagedDevice | None:
        """Returns the device which name is given in argument"""
        for _, device in enumerate(self._devices):
            if device.unique_id == uid:
                return device
        return None

    def set_priority_weight_entity(self, entity: SelectEntity):
        """Set the priority weight entity"""
        self._priority_weight_entity = entity

    @property
    def priority_weight(self) -> int:
        """Get the priority weight"""
        if self._priority_weight_entity is None:
            return 0
        return self._priority_weight_entity.current_priority_weight

    @property
    def raz_time(self) -> time:
        """Get the raz time with default to DEFAULT_RAZ_TIME"""
        return self._raz_time

    def add_device(self, device: ManagedDevice):
        """Add a new device to the list of managed device"""
        # Append or replace the device
        for i, dev in enumerate(self._devices):
            if dev.unique_id == device.unique_id:
                self._devices[i] = device
                return
        self._devices.append(device)

    def remove_device(self, unique_id: str):
        """Remove a device from the list of managed device"""
        for i, dev in enumerate(self._devices):
            if dev.unique_id == unique_id:
                self._devices.pop(i)
                return

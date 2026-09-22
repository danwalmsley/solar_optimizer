""" The data coordinator class """

import logging
import math
import time as monotonic_time
from datetime import datetime, timedelta, time
from typing import Any

from homeassistant.core import HomeAssistant, Event, EventStateChangedData
from homeassistant.components.select import SelectEntity

from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
)

from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
)

from homeassistant.util.unit_conversion import BaseUnitConverter, PowerConverter

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_BATTERY_CHARGE_RESERVE_START_SOC,
    CONF_MAXIMUM_BATTERY_CHARGE_RESERVE_POWER,
    CONF_MINIMUM_EXPORT_POWER,
    DEFAULT_BATTERY_CHARGE_RESERVE_START_SOC,
    DEFAULT_MAXIMUM_BATTERY_CHARGE_RESERVE_POWER,
    DEFAULT_MINIMUM_EXPORT_POWER,
    CONF_SWITCHING_STABILITY_SEC,
    DEFAULT_SWITCHING_STABILITY_SEC,
    DEFAULT_RAZ_TIME,
    DEFAULT_REFRESH_PERIOD_SEC,
    SOLAR_OPTIMIZER_DOMAIN,
    battery_charge_reserve_power,
    name_to_unique_id,
)
from .surplus_control import SurplusController
from .managed_device import ManagedDevice
from .simulated_annealing_algo import SimulatedAnnealingAlgorithm

_LOGGER = logging.getLogger(__name__)


def get_safe_float(hass, entity_id: str, unit: str = None):
    """Get a safe float state value for an entity.
    Return None if entity is not available"""
    if entity_id is None or not (state := hass.states.get(entity_id)) or state.state == "unknown" or state.state == "unavailable":
        return None

    try:
        float_val = float(state.state)
    except (TypeError, ValueError):
        return None

    if unit is not None and state.attributes.get("unit_of_measurement"):
        try:
            float_val = PowerConverter.convert(float_val, state.attributes["unit_of_measurement"], unit)
        except (TypeError, ValueError, KeyError):
            return None

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
        self._maximum_battery_charge_reserve_power: float = DEFAULT_MAXIMUM_BATTERY_CHARGE_RESERVE_POWER
        self._battery_charge_reserve_start_soc: float = DEFAULT_BATTERY_CHARGE_RESERVE_START_SOC
        self._minimum_export_power: float = DEFAULT_MINIMUM_EXPORT_POWER
        self._surplus = SurplusController()
        self._stability_unsub = None
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

        self._algo = SimulatedAnnealingAlgorithm(init_temp, min_temp, cooling_factor, max_iteration_number)
        self.config = config

    async def configure(self, config: ConfigEntry) -> None:
        """Configure the coordinator from configEntry of the integration"""
        values = {**config.data, **config.options}
        refresh_period_sec = values.get("refresh_period_sec") or DEFAULT_REFRESH_PERIOD_SEC
        self.update_interval = timedelta(seconds=refresh_period_sec)
        self._schedule_refresh()

        self._power_consumption_entity_id = values.get("power_consumption_entity_id")
        self._power_production_entity_id = values.get("power_production_entity_id")
        self._subscribe_to_events = values.get("subscribe_to_events")

        if self._unsub_events is not None:
            self._unsub_events()
            self._unsub_events = None

        if self._subscribe_to_events:
            tracked_entities = [
                self._power_consumption_entity_id,
                self._power_production_entity_id,
                values.get("battery_soc_entity_id"),
                values.get("battery_charge_power_entity_id"),
            ]
            self._unsub_events = async_track_state_change_event(self.hass, [entity_id for entity_id in tracked_entities if entity_id], self._async_on_change)
            config.async_on_unload(self._cleanup_events)

        self._sell_cost_entity_id = values.get("sell_cost_entity_id")
        self._buy_cost_entity_id = values.get("buy_cost_entity_id")
        self._sell_tax_percent_entity_id = values.get("sell_tax_percent_entity_id")
        self._battery_soc_entity_id = values.get("battery_soc_entity_id")
        self._battery_charge_power_entity_id = values.get("battery_charge_power_entity_id")
        self._maximum_battery_charge_reserve_power = float(
            values.get(
                CONF_MAXIMUM_BATTERY_CHARGE_RESERVE_POWER,
                DEFAULT_MAXIMUM_BATTERY_CHARGE_RESERVE_POWER,
            )
        )
        self._battery_charge_reserve_start_soc = float(
            values.get(
                CONF_BATTERY_CHARGE_RESERVE_START_SOC,
                DEFAULT_BATTERY_CHARGE_RESERVE_START_SOC,
            )
        )
        self._minimum_export_power = float(
            values.get(
                CONF_MINIMUM_EXPORT_POWER,
                DEFAULT_MINIMUM_EXPORT_POWER,
            )
        )
        self._cleanup_stability()
        self._surplus = SurplusController(float(values.get(CONF_SWITCHING_STABILITY_SEC, DEFAULT_SWITCHING_STABILITY_SEC)))
        config.async_on_unload(self._cleanup_stability)
        self._smooth_production = values.get("smooth_production") is True
        self._last_production = 0.0

        self._raz_time = datetime.strptime(values.get("raz_time") or DEFAULT_RAZ_TIME, "%H:%M").time()
        self._central_config_done = True

    async def on_ha_started(self, _) -> None:
        """Listen the homeassistant_started event to initialize the first calculation"""
        _LOGGER.info("First initialization of Solar Optimizer")

    async def _async_on_change(self, event: Event[EventStateChangedData]) -> None:
        await self.async_refresh()
        self._schedule_refresh()

    async def _async_update_data(self):
        """Select loads, then enforce surplus and switching constraints."""
        data = {}
        for device in self._devices:
            await device.expire_forced_activation()
            device.set_current_power_with_device_state()

        production = get_safe_float(self.hass, self._power_production_entity_id, "W")
        grid = get_safe_float(self.hass, self._power_consumption_entity_id, "W")
        soc = get_safe_float(self.hass, self._battery_soc_entity_id)
        battery = get_safe_float(self.hass, self._battery_charge_power_entity_id, "W")
        has_battery = bool(self._battery_charge_power_entity_id or self._battery_soc_entity_id)
        valid = production is not None and grid is not None
        if has_battery:
            valid = valid and battery is not None and soc is not None and 0 <= soc <= 100
        else:
            battery = 0
        reserve = self._effective_battery_charge_reserve_power(soc)
        effective = self._effective_power_consumption(grid, battery, soc) if valid else None
        if production is not None:
            self._last_production = round(0.5 * self._last_production + 0.5 * production)
        data.update(
            power_production=(self._last_production if self._smooth_production else production),
            power_production_brut=production,
            power_consumption=grid,
            battery_soc=soc,
            battery_charge_power=battery,
            maximum_battery_charge_reserve_power=self._maximum_battery_charge_reserve_power,
            battery_charge_reserve_start_soc=self._battery_charge_reserve_start_soc,
            effective_battery_charge_reserve_power=reserve,
            minimum_export_power=self._minimum_export_power,
            switching_stability_sec=self._surplus.interval,
            effective_power_consumption=effective,
            usable_excess_power=max(0, -effective) if effective is not None else None,
            priority_weight=self.priority_weight,
        )
        for key, entity in (("sell_cost", self._sell_cost_entity_id), ("buy_cost", self._buy_cost_entity_id), ("sell_tax_percent", self._sell_tax_percent_entity_id)):
            data[key] = get_safe_float(self.hass, entity)

        equipment = []
        for device in self._devices:
            if not device.is_enabled:
                continue  # Manual/forced loads remain ordinary household demand.
            device.set_battery_soc(soc)
            equipment.append(
                {
                    "name": device.name,
                    "current_power": device.current_power,
                    "state": device.is_active,
                    "requested_power": device.current_power,
                    "priority": device.priority,
                    "power_max": device.power_max,
                    "power_min": device.power_min,
                    "power_step": device.power_step,
                    "can_change_power": device.can_change_power,
                    "stop_reason": device.surplus_stop_reason,
                    "increase_waiting": device.is_waiting if not device.is_active else device.power_change_waiting,
                }
            )

        proposed, objective = [], None
        if valid and all(data[k] is not None for k in ("sell_cost", "buy_cost", "sell_tax_percent")):
            try:
                proposed, objective, _ = self._algo.recuit_simule(
                    self._devices,
                    effective,
                    data["power_production"],
                    data["sell_cost"],
                    data["buy_cost"],
                    data["sell_tax_percent"],
                    soc,
                    self.priority_weight,
                )
            except Exception:  # Protection must survive a failed allocation calculation.
                _LOGGER.exception("Optimizer failed; retaining only safe existing loads")
        now = monotonic_time.monotonic()
        solution = self._surplus.decide(equipment, proposed, effective, now)
        for item in solution:
            device = self.get_device_by_name(item["name"])
            target = item["requested_power"]
            previous = self._surplus.commands.get(device.name)
            cancelling_start = previous is not None and previous[1] > 0 and target == 0
            if not cancelling_start and target == item["current_power"] and device.is_active == (target > 0):
                continue
            if self._surplus.command_pending(device.name, target, now):
                continue
            try:
                if target <= 0:
                    await device.deactivate()
                elif not device.is_active:
                    await device.activate(target)
                    if device.can_change_power:
                        await device.change_requested_power(target)
                elif device.can_change_power:
                    await device.change_requested_power(target)
                device.set_requested_power(target)
            except Exception:
                # A broken device must not prevent shedding other managed loads.
                # Record the attempted command to bound retries and reserve its power.
                item["decision_reason"] = "command_failed"
                _LOGGER.exception("Failed to set %s to %s W; will recheck", device.name, target)
            self._surplus.record_command(device.name, target, now)

        # Schedule command settling as well as confirmation expiry.
        deadlines = [self._surplus.deadline] if self._surplus.deadline is not None else []
        deadlines.extend(sent + self._surplus.interval for sent, _ in self._surplus.commands.values() if sent + self._surplus.interval > now)
        self._schedule_stability(min(deadlines) if deadlines else None, now)
        data.update(
            best_solution=solution,
            best_objective=objective,
            total_power=sum(e["requested_power"] for e in solution),
            available_controlled_load_budget=self._surplus.budget,
            projected_shortfall=self._surplus.shortfall,
            device_decisions={
                e["name"]: {
                    "reason": e["decision_reason"],
                    "pending_until": (
                        (datetime.now().astimezone() + timedelta(seconds=max(0, e["pending_deadline"] - now))).isoformat()
                        if e["pending_deadline"] is not None and e["pending_deadline"] > now
                        else None
                    ),
                }
                for e in solution
            },
        )
        for device in self._devices:
            data[name_to_unique_id(device.name)] = device
        return data

    def _effective_battery_charge_reserve_power(self, soc):
        if not (self._battery_charge_power_entity_id or self._battery_soc_entity_id):
            return 0
        return battery_charge_reserve_power(
            self._maximum_battery_charge_reserve_power,
            self._battery_charge_reserve_start_soc,
            soc,
        )

    def _effective_power_consumption(self, grid, battery, soc=None):
        if grid is None or battery is None:
            return None
        return grid + battery + self._effective_battery_charge_reserve_power(soc) + self._minimum_export_power

    def _cleanup_stability(self):
        if self._stability_unsub is not None:
            self._stability_unsub()
            self._stability_unsub = None
        self._surplus.pending.clear()
        self._surplus.commands.clear()

    def _cleanup_events(self):
        if self._unsub_events is not None:
            self._unsub_events()
            self._unsub_events = None

    def _schedule_stability(self, deadline, now):
        if self._stability_unsub is not None:
            self._stability_unsub()
            self._stability_unsub = None
        if deadline is not None:
            self._stability_unsub = async_call_later(self.hass, max(0.05, deadline - now), self._async_stability_elapsed)

    async def _async_stability_elapsed(self, _):
        self._stability_unsub = None
        await self.async_refresh()
        self._schedule_refresh()

    @classmethod
    def get_coordinator(cls) -> Any:
        """Get the coordinator from the hass.data"""
        if not hasattr(SolarOptimizerCoordinator, "hass") or SolarOptimizerCoordinator.hass is None or SolarOptimizerCoordinator.hass.data.get(SOLAR_OPTIMIZER_DOMAIN) is None:
            return None

        return SolarOptimizerCoordinator.hass.data[SOLAR_OPTIMIZER_DOMAIN]["coordinator"]

    @classmethod
    def reset(cls) -> Any:
        """Reset the coordinator from the hass.data"""
        if not hasattr(SolarOptimizerCoordinator, "hass") or SolarOptimizerCoordinator.hass is None or SolarOptimizerCoordinator.hass.data.get(SOLAR_OPTIMIZER_DOMAIN) is None:
            return

        SolarOptimizerCoordinator.hass.data[SOLAR_OPTIMIZER_DOMAIN]["coordinator"] = None

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

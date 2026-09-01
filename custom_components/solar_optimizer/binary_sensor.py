"""Binary diagnostic entities for Solar Optimizer."""

import logging

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_DEVICE_CENTRAL,
    CONF_DEVICE_TYPE,
    DEVICE_MANUFACTURER,
    DOMAIN,
    INTEGRATION_MODEL,
)
from .coordinator import SolarOptimizerCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up central battery policy diagnostics."""
    if entry.data.get(CONF_DEVICE_TYPE) != CONF_DEVICE_CENTRAL:
        return

    coordinator = SolarOptimizerCoordinator.get_coordinator()
    async_add_entities([SolarOptimizerBatteryBudgetBinarySensor(coordinator)], False)


class SolarOptimizerBatteryBudgetBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Show whether flexible loads may currently use the battery budget."""

    _attr_name = "battery_budget_active"
    _attr_unique_id = "solar_optimizer_battery_budget_active"
    _attr_icon = "mdi:battery-lock-open"

    def __init__(self, coordinator: SolarOptimizerCoordinator) -> None:
        super().__init__(coordinator, context="battery_budget_active")
        self._attr_is_on = False

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if not self.coordinator or not self.coordinator.data:
            _LOGGER.debug("No coordinator data available for battery budget")
            return

        self._attr_is_on = bool(self.coordinator.data.get("battery_budget_active", False))
        self.async_write_ha_state()

    @property
    def device_info(self) -> DeviceInfo:
        """Return the central Solar Optimizer device information."""
        return DeviceInfo(
            entry_type=DeviceEntryType.SERVICE,
            identifiers={(DOMAIN, CONF_DEVICE_CENTRAL)},
            name="Solar Optimizer",
            manufacturer=DEVICE_MANUFACTURER,
            model=INTEGRATION_MODEL,
        )

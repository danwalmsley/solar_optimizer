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
) -> SolarOptimizerCoordinator:
    """Create a coordinator configured for a battery policy test."""
    coordinator = SolarOptimizerCoordinator(hass, None)
    coordinator._battery_power_strategy = strategy
    coordinator._minimum_export_power = minimum_export_power
    coordinator._battery_budget_start_soc = 100
    coordinator._battery_budget_stop_soc = 90
    coordinator._battery_budget_active = False
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

    assert coordinator._effective_power_consumption(-1500, -500) == -1300
    assert coordinator._effective_power_consumption(0, -500) == 200
    assert coordinator._effective_power_consumption(0, 500) == 700
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
    assert coordinator._effective_power_consumption(0, -500) == 200

    coordinator._update_battery_budget(100)
    assert coordinator._battery_budget_active is True
    assert coordinator._effective_power_consumption(0, 500) == 0

    coordinator._update_battery_budget(95)
    assert coordinator._battery_budget_active is True
    assert coordinator._effective_power_consumption(0, 500) == 0

    coordinator._update_battery_budget(90)
    assert coordinator._battery_budget_active is False
    assert coordinator._effective_power_consumption(0, 500) == 700


async def test_non_budget_strategy_clears_latch(hass: HomeAssistant) -> None:
    """Changing away from the budget policy cannot leave discharge enabled."""
    coordinator = create_coordinator(hass, BATTERY_POWER_STRATEGY_CHARGE_FIRST_WITH_BUDGET)
    coordinator._update_battery_budget(100)
    assert coordinator._battery_budget_active is True

    coordinator._battery_power_strategy = BATTERY_POWER_STRATEGY_CHARGE_FIRST
    coordinator._update_battery_budget(100)
    assert coordinator._battery_budget_active is False

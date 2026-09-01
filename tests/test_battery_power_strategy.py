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
    minimum_battery_charge_power: float = 100,
) -> SolarOptimizerCoordinator:
    """Create a coordinator configured for a battery policy test."""
    coordinator = SolarOptimizerCoordinator(hass, None)
    coordinator._battery_power_strategy = strategy
    coordinator._minimum_export_power = minimum_export_power
    coordinator._battery_budget_start_soc = 100
    coordinator._battery_budget_stop_soc = 90
    coordinator._battery_budget_active = False
    coordinator._minimum_battery_charge_power = minimum_battery_charge_power
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

"""Regression tests for surplus-only control, independent of stochastic selection."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solar_optimizer.const import battery_charge_reserve_power, DOMAIN
from custom_components.solar_optimizer.coordinator import SolarOptimizerCoordinator, get_safe_float
from custom_components.solar_optimizer.surplus_control import SurplusController
from custom_components.solar_optimizer import async_migrate_entry


def load(name="pool", current=1000, target=None, priority=4, variable=False, **extra):
    power = current if target is None else target
    return dict(
        name=name,
        current_power=current,
        requested_power=power,
        state=power > 0,
        priority=priority,
        can_change_power=variable,
        power_min=100,
        power_step=100,
        power_max=2000,
        stop_reason=None,
        increase_waiting=False,
        **extra
    )


@pytest.mark.parametrize("soc,reserve", [(0, 2700), (50, 2700), (59, 2700), (60, 2160), (70, 1620), (80, 1080), (90, 540), (94, 540), (95, 270), (99, 270), (100, 0), (None, 2700)])
def test_curve(soc, reserve):
    assert battery_charge_reserve_power(2700, 50, soc) == reserve


@pytest.mark.parametrize("soc", [100, 95, 90])
async def test_evening_discharge_always_counts(hass, soc):
    coordinator = SolarOptimizerCoordinator(hass, None)
    coordinator._battery_charge_power_entity_id = "sensor.battery"
    effective = coordinator._effective_power_consumption(-7, 350, soc)
    assert effective >= 343
    policy = SurplusController()
    pool = load()
    assert policy.decide([pool], [pool], effective, 0)[0]["state"]
    result = policy.decide([pool], [pool], effective, 10)
    assert not result[0]["state"]
    assert result[0]["decision_reason"] == "insufficient_surplus"


def test_short_cloud_recovers_and_new_shortage_gets_new_timer():
    policy = SurplusController()
    pool = load()
    assert policy.decide([pool], [pool], 350, 0)[0]["state"]
    assert policy.deadline == 10
    assert policy.decide([pool], [pool], -50, 9)[0]["state"]
    assert not policy.pending
    assert policy.decide([pool], [pool], 350, 10)[0]["state"]
    assert policy.decide([pool], [pool], 350, 19)[0]["state"]
    assert not policy.decide([pool], [pool], 350, 20)[0]["state"]


def test_house_can_still_discharge_after_pool_off():
    policy = SurplusController()
    pool = load()
    policy.decide([pool], [pool], 1500, 0)
    assert not policy.decide([pool], [pool], 1500, 10)[0]["state"]
    assert policy.shortfall == 500
    assert policy.budget == 0


def test_start_requires_continuous_surplus_and_accounts_for_own_load():
    policy = SurplusController()
    off, on = load(current=0), load(current=0, target=1000)
    assert not policy.decide([off], [on], -1200, 0)[0]["state"]
    assert not policy.decide([off], [on], -900, 9)[0]["state"]
    assert not policy.decide([off], [on], -1200, 10)[0]["state"]
    assert policy.decide([off], [on], -1200, 20)[0]["state"]
    policy.record_command("pool", 1000, 20)
    running = load()
    assert policy.decide([running], [running], -200, 21)[0]["state"]
    assert not policy.pending


def test_startup_transient_and_overlapping_settling():
    policy = SurplusController()
    policy.record_command("pool", 1000, 0)
    pool = load()
    assert policy.decide([pool], [pool], 300, 0)[0]["state"]
    assert not policy.decide([pool], [pool], 300, 10)[0]["state"]


def test_sensor_events_and_optimizer_randomness_cannot_restart_deficit_clock():
    policy = SurplusController()
    pool = load()
    for i in range(40):
        # Algorithm alternates between off/on, physics does not change.
        proposed = load(target=0 if i % 2 else 1000)
        assert policy.decide([pool], [proposed], 350, i / 4)[0]["state"]
    assert not policy.decide([pool], [pool], 350, 10)[0]["state"]
    assert not policy.decide([pool], [pool], 350, 11)[0]["state"]


def test_pending_shutdown_cannot_fund_new_start():
    policy = SurplusController()
    pool, other = load(), load("other", current=0, priority=2)
    proposals = [load(target=0), load("other", current=0, target=1000, priority=2)]
    result = policy.decide([pool, other], proposals, 200, 0)
    assert {i["name"]: i["requested_power"] for i in result} == {"pool": 1000, "other": 0}
    result = policy.decide([pool, other], proposals, 200, 10)
    assert all(i["requested_power"] == 0 for i in result)


def test_unacknowledged_start_reserves_power_and_is_not_repeated_during_settling():
    policy = SurplusController()
    policy.record_command("pool", 1000, 0)
    pool, other = load(current=0), load("other", current=0)
    proposed = [load(current=0, target=1000), load("other", current=0, target=1000)]
    result = policy.decide([pool, other], proposed, -1500, 1)
    assert {i["name"]: i["requested_power"] for i in result} == {"pool": 1000, "other": 0}
    assert policy.command_pending("pool", 1000, 1)
    result = policy.decide([pool, other], proposed, -1500, 11)
    assert {i["name"]: i["requested_power"] for i in result}["other"] == 0


@pytest.mark.parametrize("reason", ["minimum_soc", "maximum_daily_runtime", "unusable"])
def test_hard_stop_bypasses_settling(reason):
    policy = SurplusController()
    policy.record_command("pool", 1000, 0)
    pool = load()
    pool["stop_reason"] = reason
    result = policy.decide([pool], [pool], -5000, 1)
    assert result[0]["requested_power"] == 0
    assert result[0]["decision_reason"] == reason


def test_minimum_off_and_power_change_interval():
    policy = SurplusController()
    pool = load(current=0)
    pool["increase_waiting"] = True
    proposal = load(current=0, target=1000)
    assert not policy.decide([pool], [proposal], -5000, 0)[0]["state"]
    assert not policy.decide([pool], [proposal], -5000, 20)[0]["state"]
    pool["increase_waiting"] = False
    assert not policy.decide([pool], [proposal], -5000, 21)[0]["state"]
    assert policy.decide([pool], [proposal], -5000, 31)[0]["state"]


def test_priority_shedding_and_variable_steps():
    policy = SurplusController()
    high, low = load("high", priority=1), load("low", priority=16, variable=True)
    policy.decide([high, low], [high, low], 250, 0)
    result = policy.decide([high, low], [high, low], 250, 10)
    assert {i["name"]: i["requested_power"] for i in result} == {"high": 1000, "low": 700}


def test_variable_reduction_respects_setpoint_timer_by_stopping():
    policy = SurplusController()
    device = load(variable=True)
    device["increase_waiting"] = True
    policy.decide([device], [device], 250, 0)
    assert policy.decide([device], [device], 250, 10)[0]["requested_power"] == 0


def test_invalid_sensors_stop_after_interval_and_prevent_starts():
    policy = SurplusController()
    pool, off = load(), load("off", current=0)
    proposed = [pool, load("off", current=0, target=1000)]
    result = policy.decide([pool, off], proposed, None, 0)
    assert {i["name"]: i["requested_power"] for i in result} == {"pool": 1000, "off": 0}
    result = policy.decide([pool, off], proposed, None, 10)
    assert all(i["requested_power"] == 0 for i in result)
    assert all(i["decision_reason"] == "invalid_sensor" for i in result)


def test_disabled_interval_and_removed_device_cleanup():
    policy = SurplusController(0)
    assert not policy.decide([load()], [load()], 100, 0)[0]["state"]
    policy.record_command("pool", 0, 0)
    policy.decide([], [], 0, 1)
    assert not policy.pending and not policy.commands


async def test_full_battery_and_charge_refusal(hass):
    coordinator = SolarOptimizerCoordinator(hass, None)
    coordinator._battery_charge_power_entity_id = "sensor.battery"
    assert coordinator._effective_power_consumption(-1600, 0, 100) == -1600
    assert coordinator._effective_power_consumption(-1600, 0, 95) == -1330
    # Less than 2700W solar available: pump may not use the battery's reserve.
    assert coordinator._effective_power_consumption(0, -1500, 50) == 1200


async def test_no_battery_uses_no_reserve(hass):
    coordinator = SolarOptimizerCoordinator(hass, None)
    assert coordinator._effective_power_consumption(-1500, 0) == -1500


@pytest.mark.parametrize("state", ["unknown", "unavailable", "nonsense", "nan", "inf"])
async def test_invalid_power_reading(hass, state):
    hass.states.async_set("sensor.test_power", state)
    assert get_safe_float(hass, "sensor.test_power", "W") is None


async def test_power_unit_conversion(hass):
    hass.states.async_set("sensor.test_power", "-1.2", {"device_class": "power", "unit_of_measurement": "kW"})
    assert get_safe_float(hass, "sensor.test_power", "W") == -1200


@pytest.mark.parametrize("minor", [0, 1])
async def test_migration_data_options_and_idempotence(hass, minor):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=minor,
        data={
            "device_type": "central_config",
            "battery_power_strategy": "charge_first_with_budget",
            "battery_budget_start_soc": 100,
            "battery_budget_stop_soc": 90,
            "decision_reversal_hold_sec": 30,
            "power_deficit_confirmation_sec": 50,
            "maximum_battery_charge_reserve_power": 2700,
            "battery_soc_entity_id": "sensor.soc",
        },
        options={"battery_budget_stop_soc": 95, "minimum_export_power": 100},
    )
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    assert entry.minor_version == 2
    assert entry.data["switching_stability_sec"] == 10
    assert entry.options == {"minimum_export_power": 100, "switching_stability_sec": 10}
    assert entry.data["battery_soc_entity_id"] == "sensor.soc"
    assert "battery_power_strategy" not in entry.data
    assert "decision_reversal_hold_sec" not in entry.data
    hass.config_entries.async_update_entry(entry, data={**entry.data, "switching_stability_sec": 30})
    await async_migrate_entry(hass, entry)
    assert entry.data["switching_stability_sec"] == 30


def fake_device():
    device = MagicMock()
    device.name = "pool"
    device.is_enabled = True
    device.current_power = 1000
    device.is_active = True
    device.power_max = 1000
    device.power_min = 1000
    device.power_step = 1000
    device.priority = 4
    device.can_change_power = False
    device.surplus_stop_reason = None
    device.power_change_waiting = False
    device.is_waiting = True
    device.expire_forced_activation = AsyncMock()
    device.activate = AsyncMock()
    device.deactivate = AsyncMock()
    return device


async def configured_coordinator(hass):
    coordinator = SolarOptimizerCoordinator(hass, None)
    coordinator._power_production_entity_id = "sensor.pv"
    coordinator._power_consumption_entity_id = "sensor.grid"
    coordinator._battery_soc_entity_id = "sensor.soc"
    coordinator._battery_charge_power_entity_id = "sensor.battery"
    hass.states.async_set("sensor.pv", 1100)
    hass.states.async_set("sensor.grid", 0)
    hass.states.async_set("sensor.soc", 100)
    hass.states.async_set("sensor.battery", 350)
    coordinator._devices = [fake_device()]
    return coordinator


async def test_coordinator_missing_tariffs_still_stops_and_does_not_force_offpeak(hass):
    coordinator = await configured_coordinator(hass)
    device = coordinator._devices[0]
    device.should_be_forced_offpeak = True
    with patch("custom_components.solar_optimizer.coordinator.monotonic_time.monotonic", return_value=0):
        await coordinator._async_update_data()
    device.deactivate.assert_not_called()
    with patch("custom_components.solar_optimizer.coordinator.monotonic_time.monotonic", return_value=10):
        data = await coordinator._async_update_data()
    device.deactivate.assert_awaited_once()
    device.activate.assert_not_called()
    assert data["device_decisions"]["pool"]["reason"] == "insufficient_surplus"
    coordinator._cleanup_stability()


async def test_coordinator_invalid_battery_is_not_zero(hass):
    coordinator = await configured_coordinator(hass)
    hass.states.async_set("sensor.battery", "unavailable")
    with patch("custom_components.solar_optimizer.coordinator.monotonic_time.monotonic", return_value=0):
        data = await coordinator._async_update_data()
    assert data["battery_charge_power"] is None
    assert data["effective_power_consumption"] is None
    with patch("custom_components.solar_optimizer.coordinator.monotonic_time.monotonic", return_value=10):
        await coordinator._async_update_data()
    coordinator._devices[0].deactivate.assert_awaited_once()
    coordinator._cleanup_stability()


async def test_manual_device_never_controlled(hass):
    coordinator = await configured_coordinator(hass)
    device = coordinator._devices[0]
    device.is_enabled = False
    coordinator._surplus.interval = 0
    data = await coordinator._async_update_data()
    assert data["best_solution"] == []
    device.deactivate.assert_not_called()
    coordinator._cleanup_stability()


async def test_deadline_callback_recalculates_without_sensor_event_and_cleanup(hass):
    coordinator = await configured_coordinator(hass)
    callbacks = []
    with patch(
        "custom_components.solar_optimizer.coordinator.async_call_later", side_effect=lambda hass, delay, callback: callbacks.append((delay, callback)) or MagicMock()
    ) as schedule:
        with patch("custom_components.solar_optimizer.coordinator.monotonic_time.monotonic", return_value=0):
            await coordinator._async_update_data()
        assert callbacks[0][0] == 10
        with patch("custom_components.solar_optimizer.coordinator.monotonic_time.monotonic", return_value=10):
            await callbacks[0][1](None)
        coordinator._devices[0].deactivate.assert_awaited_once()
        cancel = coordinator._stability_unsub
        coordinator._cleanup_stability()
        cancel.assert_called_once()
    assert not coordinator._surplus.pending and not coordinator._surplus.commands


def test_pending_start_is_cancelled_when_surplus_disappears():
    policy = SurplusController()
    policy.record_command("pool", 1000, 0)
    off = load(current=0)
    assert policy.decide([off], [], 0, 1)[0]["requested_power"] == 1000
    assert policy.decide([off], [], 0, 11)[0]["requested_power"] == 0


def test_start_confirmation_survives_stochastic_proposals():
    policy = SurplusController()
    off, on = load(current=0), load(current=0, target=1000)
    assert not policy.decide([off], [on], -1500, 0)[0]["state"]
    for second in range(1, 10):
        assert not policy.decide([off], [off if second % 2 else on], -1500, second)[0]["state"]
    assert policy.decide([off], [off], -1500, 10)[0]["state"]


def test_immersion_heater_does_not_stop_pool_when_remaining_surplus_covers_it():
    policy = SurplusController()
    # PV 4000W, other house/immersion 2400W, pool already using 1000W.
    pool = load()
    for second in range(30):
        assert policy.decide([pool], [pool], -600, second)[0]["state"]
    assert not policy.pending


def test_reduction_recovers_before_confirmation():
    policy = SurplusController()
    device = load(variable=True)
    assert policy.decide([device], [device], 250, 0)[0]["requested_power"] == 1000
    assert policy.decide([device], [device], -50, 9)[0]["requested_power"] == 1000
    assert not policy.pending


def test_zero_delay_allocations_never_exceed_available_power():
    import random

    rng = random.Random(314)
    for _ in range(300):
        equipment = [load(str(i), current=rng.choice([0, 500, 1000]), priority=rng.choice([1, 4, 16]), variable=True) for i in range(4)]
        proposed = [dict(e, requested_power=rng.choice([0, 500, 1000]), state=True) for e in equipment]
        effective = rng.randint(-3000, 3000)
        policy = SurplusController(0)
        result = policy.decide(equipment, proposed, effective, 0)
        total = sum(e["requested_power"] for e in result)
        assert total == 0 or effective + total - sum(e["current_power"] for e in equipment) <= 0


async def test_coordinator_optimizer_exception_does_not_bypass_guard(hass):
    coordinator = await configured_coordinator(hass)
    for name in ("sell_cost", "buy_cost", "sell_tax_percent"):
        setattr(coordinator, "_" + name + "_entity_id", "sensor." + name)
        hass.states.async_set("sensor." + name, 1)
    coordinator._surplus.interval = 0
    with patch.object(coordinator._algo, "recuit_simule", side_effect=RuntimeError("bad selection")):
        await coordinator._async_update_data()
    coordinator._devices[0].deactivate.assert_awaited_once()
    coordinator._cleanup_stability()


@pytest.mark.parametrize("reason", ["minimum_soc", "unusable", "maximum_daily_runtime"])
async def test_coordinator_hard_stop_not_repeated_on_event_storm(hass, reason):
    coordinator = await configured_coordinator(hass)
    device = coordinator._devices[0]
    device.surplus_stop_reason = reason
    for second in range(10):
        with patch("custom_components.solar_optimizer.coordinator.monotonic_time.monotonic", return_value=second):
            await coordinator._async_update_data()
    device.deactivate.assert_awaited_once()
    coordinator._cleanup_stability()


async def test_missing_grid_and_pv_stop_existing_loads(hass):
    coordinator = await configured_coordinator(hass)
    hass.states.async_set("sensor.grid", "unavailable")
    hass.states.async_set("sensor.pv", "unavailable")
    coordinator._surplus.interval = 0
    data = await coordinator._async_update_data()
    coordinator._devices[0].deactivate.assert_awaited_once()
    assert data["device_decisions"]["pool"]["reason"] == "invalid_sensor"
    coordinator._cleanup_stability()


async def test_migration_removes_only_obsolete_diagnostics(hass):
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    entry = MockConfigEntry(domain=DOMAIN, version=2, minor_version=1, data={"device_type": "central_config"})
    entry.add_to_hass(hass)
    obsolete = registry.async_get_or_create("binary_sensor", DOMAIN, "solar_optimizer_battery_budget_active", config_entry=entry)
    keep = registry.async_get_or_create("sensor", DOMAIN, "solar_optimizer_battery_soc", config_entry=entry)
    await async_migrate_entry(hass, entry)
    assert registry.async_get(obsolete.entity_id) is None
    assert registry.async_get(keep.entity_id) is not None


async def test_failed_device_command_does_not_block_other_shutdowns(hass):
    coordinator = await configured_coordinator(hass)
    first = coordinator._devices[0]
    second = fake_device()
    second.name = "second"
    coordinator._devices.append(second)
    first.deactivate.side_effect = RuntimeError("device offline")
    hass.states.async_set("sensor.battery", 2500)
    with patch("custom_components.solar_optimizer.coordinator.monotonic_time.monotonic", return_value=0):
        await coordinator._async_update_data()
    with patch("custom_components.solar_optimizer.coordinator.monotonic_time.monotonic", return_value=10):
        data = await coordinator._async_update_data()
    first.deactivate.assert_awaited_once()
    second.deactivate.assert_awaited_once()
    assert data["device_decisions"]["pool"]["reason"] == "command_failed"
    assert coordinator._stability_unsub is not None
    coordinator._cleanup_stability()


async def test_configure_reads_stored_options_and_unregisters_events(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=2,
        data={
            "device_type": "central_config",
            "subscribe_to_events": True,
            "power_consumption_entity_id": "sensor.grid",
            "power_production_entity_id": "sensor.pv",
            "switching_stability_sec": 10,
        },
        options={"switching_stability_sec": 30, "maximum_battery_charge_reserve_power": 800},
    )
    entry.add_to_hass(hass)
    coordinator = SolarOptimizerCoordinator(hass, None)
    cancel = MagicMock()
    with patch("custom_components.solar_optimizer.coordinator.async_track_state_change_event", return_value=cancel):
        await coordinator.configure(entry)
    assert coordinator._surplus.interval == 30
    assert coordinator._maximum_battery_charge_reserve_power == 800
    coordinator._cleanup_events()
    coordinator._cleanup_events()
    cancel.assert_called_once()
    coordinator._cleanup_stability()

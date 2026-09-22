""" Testing the ConfigFlow """

# pylint: disable=unused-argument, wildcard-import, unused-wildcard-import

import itertools
import pytest
from datetime import time

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData

from custom_components.solar_optimizer import config_flow

from custom_components.solar_optimizer.const import *
from custom_components.solar_optimizer.coordinator import SolarOptimizerCoordinator


async def test_empty_config(hass: HomeAssistant, reset_coordinator):
    """Test an empty config. This should not work"""
    _result = await hass.config_entries.flow.async_init(
        config_flow.DOMAIN, context={"source": "user"}
    )

    assert _result["step_id"] == "device_central"
    assert _result["type"] == FlowResultType.FORM
    assert _result["errors"] == {}

    with pytest.raises(InvalidData) as err:
        await hass.config_entries.flow.async_configure(
            _result["flow_id"], user_input={}
        )

    assert err.typename == "InvalidData"
    assert err.value.error_message == "required key not provided"


@pytest.mark.parametrize(
    "power_consumption,power_production,sell_cost,buy_cost,raz_time,battery_soc",
    [
        (
            "sensor.power_consumption",
            "sensor.power_production",
            "input_number.sell_cost",
            "input_number.buy_cost",
            "00:00",
            None,
        ),
        (
            "input_number.power_consumption",
            "input_number.power_production",
            "input_number.sell_cost",
            "input_number.buy_cost",
            "04:00",
            None,
        ),
        (
            "sensor.power_consumption",
            "sensor.power_production",
            "input_number.sell_cost",
            "input_number.buy_cost",
            "00:00",
            "sensor.battery_soc",
        ),
        (
            "input_number.power_consumption",
            "input_number.power_production",
            "input_number.sell_cost",
            "input_number.buy_cost",
            "04:00",
            "input_number.battery_soc",
        ),
    ],
)
async def test_central_config_inputs(
    hass: HomeAssistant,
    skip_hass_states_get,
    reset_coordinator,
    power_consumption,
    power_production,
    sell_cost,
    buy_cost,
    raz_time,
    battery_soc,
):
    """Test a combinaison of config_flow without battery configuration"""

    _result = await hass.config_entries.flow.async_init(
        config_flow.DOMAIN, context={"source": "user"}
    )

    assert _result["step_id"] == "device_central"
    assert _result["type"] == FlowResultType.FORM
    assert _result["errors"] == {}

    user_input = {
        "refresh_period_sec": 300,
        "power_consumption_entity_id": power_consumption,
        "power_production_entity_id": power_production,
        "sell_cost_entity_id": sell_cost,
        "buy_cost_entity_id": buy_cost,
        "sell_tax_percent_entity_id": "input_number.tax_percent",
        "raz_time": raz_time,
    }

    if battery_soc:
        user_input["battery_soc_entity_id"] = battery_soc

    result = await hass.config_entries.flow.async_configure(
        _result["flow_id"],
        user_input
    )
    await hass.async_block_till_done()
    if result.get("step_id") == "battery_reserve_review":
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] == FlowResultType.CREATE_ENTRY
    data = result.get("data")
    assert data is not None

    for key, value in user_input.items():
        assert data.get(key) == value

    if battery_soc:
        assert data["battery_soc_entity_id"] == battery_soc

    assert data["smooth_production"]

    assert result["title"] == "Configuration"


async def test_default_values_central_config(
    hass: HomeAssistant, skip_hass_states_get, reset_coordinator
):
    """Test a combinaison of config_flow with battery configuration"""
    _result = await hass.config_entries.flow.async_init(
        config_flow.DOMAIN, context={"source": "user"}
    )

    assert _result["step_id"] == "device_central"
    assert _result["type"] == FlowResultType.FORM
    assert _result["errors"] == {}

    user_input = {
        # "refresh_period_sec": 300,
        # "raz_time": "04:00",
        # "smooth_production": True,
        # battery_soc_entity_id: None,
        "power_consumption_entity_id": "input_number.power_consumption",
        "power_production_entity_id": "input_number.power_production",
        "sell_cost_entity_id": "input_number.sell_cost",
        "buy_cost_entity_id": "input_number.buy_cost",
        "sell_tax_percent_entity_id": "input_number.tax_percent",
    }

    result = await hass.config_entries.flow.async_configure(
        _result["flow_id"], user_input
    )
    await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    data = result.get("data")
    assert data is not None

    for key, value in user_input.items():
        assert data.get(key) == value

    assert data.get("refresh_period_sec") == 300
    assert data.get("raz_time") == DEFAULT_RAZ_TIME

    assert data["smooth_production"]
    assert data.get("battery_soc_entity_id") is None
    assert data.get(CONF_MAXIMUM_BATTERY_CHARGE_RESERVE_POWER) == 2700
    assert data.get(CONF_BATTERY_CHARGE_RESERVE_START_SOC) == 50
    assert data.get(CONF_MINIMUM_EXPORT_POWER) == 0
    assert data.get(CONF_SWITCHING_STABILITY_SEC) == 10

    assert result["title"] == "Configuration"


def test_obsolete_controls_are_absent():
    from custom_components.solar_optimizer.config_schema import central_config_schema
    keys = {str(key) for key in central_config_schema.schema}
    assert "switching_stability_sec" in keys
    assert not keys.intersection({"battery_power_strategy", "battery_budget_start_soc",
        "battery_budget_stop_soc", "decision_reversal_hold_sec", "power_deficit_confirmation_sec"})


async def test_battery_charge_reserve_curve_review(
    hass: HomeAssistant, skip_hass_states_get, reset_coordinator
):
    """A configured taper is previewed before the central entry is saved."""
    result = await hass.config_entries.flow.async_init(
        config_flow.DOMAIN, context={"source": "user"}
    )
    user_input = {
        CONF_POWER_CONSUMPTION_ENTITY_ID: "input_number.power_consumption",
        CONF_POWER_PRODUCTION_ENTITY_ID: "input_number.power_production",
        CONF_SELL_COST_ENTITY_ID: "input_number.sell_cost",
        CONF_BUY_COST_ENTITY_ID: "input_number.buy_cost",
        CONF_SELL_TAX_PERCENT_ENTITY_ID: "input_number.tax_percent",
        CONF_MAXIMUM_BATTERY_CHARGE_RESERVE_POWER: 2700,
        CONF_BATTERY_SOC_ENTITY_ID: "sensor.battery_soc",
        CONF_BATTERY_CHARGE_POWER_ENTITY_ID: "sensor.battery_power",
        CONF_BATTERY_CHARGE_RESERVE_START_SOC: 50,
    }

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=user_input
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "battery_reserve_review"
    curve = result["description_placeholders"]["battery_reserve_curve"]
    assert "| 50–59% | 2700 W |" in curve
    assert "| 90–94% | 540 W |" in curve
    assert "| 95–99% | 270 W |" in curve
    assert "| 100% | 0 W" in curve

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_BATTERY_CHARGE_RESERVE_START_SOC] == 50


async def test_invalid_battery_charge_reserve_start_soc(
    hass: HomeAssistant, skip_hass_states_get, reset_coordinator
):
    """The taper must start below full charge."""
    result = await hass.config_entries.flow.async_init(
        config_flow.DOMAIN, context={"source": "user"}
    )
    user_input = {
        CONF_POWER_CONSUMPTION_ENTITY_ID: "input_number.power_consumption",
        CONF_POWER_PRODUCTION_ENTITY_ID: "input_number.power_production",
        CONF_SELL_COST_ENTITY_ID: "input_number.sell_cost",
        CONF_BUY_COST_ENTITY_ID: "input_number.buy_cost",
        CONF_SELL_TAX_PERCENT_ENTITY_ID: "input_number.tax_percent",
        CONF_BATTERY_CHARGE_RESERVE_START_SOC: 100,
    }

    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(result["flow_id"], user_input=user_input)


async def test_wrong_raz_time(
    hass: HomeAssistant, skip_hass_states_get, reset_coordinator
):
    """Test an empty config. This should not work"""
    _result = await hass.config_entries.flow.async_init(
        config_flow.DOMAIN, context={"source": "user"}
    )

    assert _result["step_id"] == "device_central"
    assert _result["type"] == FlowResultType.FORM
    assert _result["errors"] == {}

    user_input = {
        # "refresh_period_sec": 300,
        "raz_time": "04h00",  # Not valid !
        # "smooth_production": True,
        # battery_soc_entity_id: None,
        "power_consumption_entity_id": "input_number.power_consumption",
        "power_production_entity_id": "input_number.power_production",
        "sell_cost_entity_id": "input_number.sell_cost",
        "buy_cost_entity_id": "input_number.buy_cost",
        "sell_tax_percent_entity_id": "input_number.tax_percent",
    }

    _result = await hass.config_entries.flow.async_configure(
        _result["flow_id"], user_input=user_input
    )

    assert _result["step_id"] == "device_central"
    assert _result["type"] == FlowResultType.FORM
    assert _result["errors"] == {"raz_time": "format_time_invalid"}


async def test_simple_device_config(
    hass: HomeAssistant, skip_hass_states_get, init_solar_optimizer_central_config
):
    """Test a simple device configuration"""
    result = await hass.config_entries.flow.async_init(
        config_flow.DOMAIN, context={"source": "user"}
    )

    assert result["step_id"] == "user"
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_DEVICE_TYPE: CONF_DEVICE}
    )

    assert result["step_id"] == "device"
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {}

    user_input = {
        CONF_NAME: "Equipement A",
        CONF_ENTITY_ID: "input_boolean.fake_device_a",
        CONF_POWER_MAX: "{{ 1000 }}",
        CONF_CHECK_USABLE_TEMPLATE: "{{ True }}",
        CONF_CHECK_ACTIVE_TEMPLATE: "{{ False }}",
        CONF_DURATION_MIN: 0.3,
        CONF_DURATION_STOP_MIN: 0.1,
        CONF_ACTION_MODE: CONF_ACTION_MODE_ACTION,
        CONF_ACTIVATION_SERVICE: "input_boolean/turn_on",
        CONF_DEACTIVATION_SERVICE: "input_boolean/turn_off",
        CONF_BATTERY_SOC_THRESHOLD: "{{ 50 }}",
        CONF_MAX_ON_TIME_PER_DAY_MIN: "{{ 10 }}",
        CONF_MIN_ON_TIME_PER_DAY_MIN: "{{ 1 }}",
        CONF_OFFPEAK_TIME: "22:00",
    }

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input
    )
    await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    data = result.get("data")
    assert data is not None

    assert data.get(CONF_NAME) == "Equipement A"
    assert data.get(CONF_POWER_MAX) == "{{ 1000 }}"
    assert data.get(CONF_POWER_MIN) is None
    assert data.get(CONF_POWER_STEP) is None
    assert data.get(CONF_CHECK_USABLE_TEMPLATE) == "{{ True }}"
    assert data.get(CONF_CHECK_ACTIVE_TEMPLATE) == "{{ False }}"
    assert data.get(CONF_DURATION_MIN) == 0.3
    assert data.get(CONF_DURATION_STOP_MIN) == 0.1
    assert data.get(CONF_DURATION_POWER_MIN) is None
    assert data.get(CONF_ACTION_MODE) == CONF_ACTION_MODE_ACTION
    assert data.get(CONF_ACTIVATION_SERVICE) == "input_boolean/turn_on"
    assert data.get(CONF_DEACTIVATION_SERVICE) == "input_boolean/turn_off"
    assert data.get(CONF_CONVERT_POWER_DIVIDE_FACTOR) is None
    assert data.get(CONF_CHANGE_POWER_SERVICE) is None
    assert data.get(CONF_POWER_ENTITY_ID) is None
    assert data.get(CONF_BATTERY_SOC_THRESHOLD) == "{{ 50 }}"
    assert data.get(CONF_MAX_ON_TIME_PER_DAY_MIN) == "{{ 10 }}"
    assert data.get(CONF_MIN_ON_TIME_PER_DAY_MIN) == "{{ 1 }}"
    assert data.get(CONF_OFFPEAK_TIME) == "22:00"


async def test_powered_device_config(
    hass: HomeAssistant, skip_hass_states_get, init_solar_optimizer_central_config
):
    """Test a simple device configuration"""
    result = await hass.config_entries.flow.async_init(
        config_flow.DOMAIN, context={"source": "user"}
    )

    assert result["step_id"] == "user"
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_DEVICE_TYPE: CONF_POWERED_DEVICE}
    )

    assert result["step_id"] == "powered_device"
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {}

    user_input = {
        CONF_NAME: "Equipement A",
        CONF_ENTITY_ID: "input_boolean.fake_device_a",
        CONF_POWER_MAX: "1000",
        CONF_POWER_MIN: 100,
        CONF_POWER_STEP: 150,
        CONF_CHECK_USABLE_TEMPLATE: "{{ True }}",
        CONF_CHECK_ACTIVE_TEMPLATE: "{{ False }}",
        CONF_DURATION_MIN: 0.3,
        CONF_DURATION_STOP_MIN: 0.1,
        CONF_DURATION_POWER_MIN: 3,
        CONF_ACTION_MODE: CONF_ACTION_MODE_ACTION,
        CONF_ACTIVATION_SERVICE: "input_boolean/turn_on",
        CONF_DEACTIVATION_SERVICE: "input_boolean/turn_off",
        CONF_CONVERT_POWER_DIVIDE_FACTOR: 6,
        CONF_CHANGE_POWER_SERVICE: "input_number/set_value",
        CONF_POWER_ENTITY_ID: "input_number.tesla_amps",
        CONF_BATTERY_SOC_THRESHOLD: "50",
        CONF_MAX_ON_TIME_PER_DAY_MIN: "10",
        CONF_MIN_ON_TIME_PER_DAY_MIN: "1",
        CONF_OFFPEAK_TIME: "22:00",
    }

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input
    )
    await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    data = result.get("data")
    assert data is not None

    assert data.get(CONF_NAME) == "Equipement A"
    assert data.get(CONF_POWER_MAX) == "1000"
    assert data.get(CONF_POWER_MIN) == 100
    assert data.get(CONF_POWER_STEP) == 150
    assert data.get(CONF_CHECK_USABLE_TEMPLATE) == "{{ True }}"
    assert data.get(CONF_CHECK_ACTIVE_TEMPLATE) == "{{ False }}"
    assert data.get(CONF_DURATION_MIN) == 0.3
    assert data.get(CONF_DURATION_STOP_MIN) == 0.1
    assert data.get(CONF_DURATION_POWER_MIN) == 3
    assert data.get(CONF_ACTION_MODE) == CONF_ACTION_MODE_ACTION
    assert data.get(CONF_ACTIVATION_SERVICE) == "input_boolean/turn_on"
    assert data.get(CONF_DEACTIVATION_SERVICE) == "input_boolean/turn_off"
    assert data.get(CONF_CONVERT_POWER_DIVIDE_FACTOR) == 6
    assert data.get(CONF_CHANGE_POWER_SERVICE) == "input_number/set_value"
    assert data.get(CONF_POWER_ENTITY_ID) == "input_number.tesla_amps"
    assert data.get(CONF_BATTERY_SOC_THRESHOLD) == "50"
    assert data.get(CONF_MAX_ON_TIME_PER_DAY_MIN) == "10"
    assert data.get(CONF_MIN_ON_TIME_PER_DAY_MIN) == "1"
    assert data.get(CONF_OFFPEAK_TIME) == "22:00"

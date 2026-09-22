""" Device with power Unit test module"""

from unittest.mock import call, patch, PropertyMock, ANY
from datetime import datetime, timedelta

from homeassistant.setup import async_setup_component
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.components.select import DOMAIN as SELECT_DOMAIN


from .commons import *  # pylint: disable=wildcard-import, unused-wildcard-import
from custom_components.solar_optimizer.managed_device import ACTION_ACTIVATE, ACTION_DEACTIVATE, ACTION_CHANGE_POWER


async def test_power_device(
    hass: HomeAssistant,
    init_solar_optimizer_central_config,
):
    """Testing power device functionality"""

    fake_device_a = await create_test_input_boolean(hass, "fake_device_a", "fake device a")
    assert fake_device_a is not None
    assert fake_device_a.state == STATE_OFF

    fake_amps_number = await create_test_input_number(hass, "fake_amps_number", "fake amps number")
    assert fake_amps_number is not None
    assert fake_amps_number.state == 0

    entry_a = MockConfigEntry(
        domain=DOMAIN,
        title="Power Device A",
        unique_id="powerDeviceAUniqueId",
        data={
            CONF_NAME: "Power Device A",
            CONF_DEVICE_TYPE: CONF_POWERED_DEVICE,
            CONF_ENTITY_ID: "input_boolean.fake_device_a",
            CONF_POWER_ENTITY_ID: "input_number.fake_amps_number",
            CONF_POWER_MAX: 1000,
            CONF_POWER_MIN: 100,
            CONF_POWER_STEP: 100,
            CONF_CHECK_USABLE_TEMPLATE: "{{ True }}",
            CONF_DURATION_MIN: 10,
            CONF_DURATION_STOP_MIN: 5,
            CONF_DURATION_POWER_MIN: 2,
            CONF_ACTION_MODE: CONF_ACTION_MODE_ACTION,
            CONF_ACTIVATION_SERVICE: "input_boolean/turn_on",
            CONF_DEACTIVATION_SERVICE: "input_boolean/turn_off",
            CONF_CHANGE_POWER_SERVICE: "input_number/set_value",
            CONF_CONVERT_POWER_DIVIDE_FACTOR: 10,
        },
    )

    device = await create_managed_device(
        hass,
        entry_a,
        "power_device_a",
    )
    assert device is not None
    assert device.name == "Power Device A"
    assert device.power_max == 1000
    assert device.power_min == 100
    assert device.power_step == 100
    assert device.power_entity_id == "input_number.fake_amps_number"
    assert device.duration_sec == 600
    assert device.duration_stop_sec == 300
    assert device.duration_power_sec == 120
    assert device._action_mode == CONF_ACTION_MODE_ACTION
    assert device._activation_service == "input_boolean/turn_on"
    assert device._deactivation_service == "input_boolean/turn_off"
    assert device._change_power_service == "input_number/set_value"
    assert device.convert_power_divide_factor == 10
    assert device.battery_soc_threshold == 0
    assert device.max_on_time_per_day_sec == 86400  # 60 * 24 * 60
    assert device.min_on_time_per_day_sec == 0
    assert device.offpeak_time == None
    assert device.entity_id == "input_boolean.fake_device_a"
    assert device.unique_id == "power_device_a"
    assert device.is_active is False
    assert device.is_usable is True
    assert device.can_change_power is True
    assert device.priority == 4

    # get the SO switch entity
    device_a_switch = search_entity(hass, "switch.solar_optimizer_power_device_a", SWITCH_DOMAIN)
    assert device_a_switch is not None

    # get the SO priority entity
    priority_weight_entity = search_entity(hass, "select.solar_optimizer_priority_weight", SELECT_DOMAIN)
    assert priority_weight_entity is not None
    assert priority_weight_entity.name == "Priority weight"
    assert priority_weight_entity.current_option == PRIORITY_WEIGHT_NULL
    assert priority_weight_entity.options == PRIORITY_WEIGHTS

    # 1. Test the next_date_available and next_date_available_power
    tz = get_tz(hass)  # pylint: disable=invalid-name
    now: datetime = datetime.now(tz=tz)
    assert (device.next_date_available.astimezone(tz) - now).total_seconds() < 1
    assert (device.next_date_available_power.astimezone(tz) - now).total_seconds() < 1

    coordinator = SolarOptimizerCoordinator.get_coordinator()

    side_effects = SideEffects(
        {
            "sensor.fake_power_consumption": State("sensor.fake_power_consumption", -500),
            "sensor.fake_power_production": State("sensor.fake_power_production", 500),
            "sensor.fake_battery_charge_power": State("sensor.fake_battery_charge_power", 0),
            "input_number.fake_sell_cost": State("input_number.fake_sell_cost", 1),
            "input_number.fake_buy_cost": State("input_number.fake_buy_cost", 1),
            "input_number.fake_sell_tax_percent": State("input_number.fake_sell_tax_percent", 0),
            "sensor.fake_battery_soc": State("sensor.fake_battery_soc", 0),
        },
        State("unknown.entity_id", "unknown"),
    )

    # 2. starts the algorithm at now
    # fmt:off
    # patch("custom_components.solar_optimizer.managed_device.do_service_action", autospec=True) as mock_do_service_action, \
    # patch("custom_components.solar_optimizer.managed_device.ManagedDevice.change_requested_power", autospec=True) as mock_change_requested_power:
    device._set_now(now)
    with patch("homeassistant.core.StateMachine.get", side_effect=side_effects.get_side_effects()), \
         patch("homeassistant.core.EventBus.fire") as mock_fire:
    # fmt:on
        assert device.check_usable() is True

        calculated_data = await coordinator._async_update_data()
        await hass.async_block_till_done()

        assert fake_device_a.state == STATE_ON

        assert calculated_data["total_power"] == 500
        assert calculated_data["power_device_a"].is_waiting is True
        assert calculated_data["power_device_a"].requested_power == 500
        assert calculated_data["power_device_a"].current_power == 0
        assert calculated_data["power_device_a"].is_active is False # it was not activated
        assert calculated_data["power_device_a"].is_usable is False # no more usable cause waiting
        assert calculated_data["power_device_a"].is_waiting is True
        assert (calculated_data["power_device_a"].next_date_available.astimezone(tz) - now).total_seconds() >= 600  # 10 minutes
        assert (calculated_data["power_device_a"].next_date_available_power.astimezone(tz) - now).total_seconds() >= 120

        assert calculated_data["best_solution"][0]["name"] == "Power Device A"
        assert calculated_data["best_solution"][0]["state"] is True
        assert calculated_data["best_solution"][0]["current_power"] == 0 # calculated before the activation
        assert calculated_data["best_solution"][0]["requested_power"] == 500

        # there is a cache in template evaluation which prevent this test to work
        # assert device.is_active is True
        # assert device_a_switch.is_on is True
        assert fake_device_a.state == STATE_ON
        assert fake_amps_number.state == 50

        # check hass.bus.fire has been called
        assert mock_fire.call_count == 2
        mock_fire.assert_has_calls(
            [
                call(
                    event_type=EVENT_TYPE_SOLAR_OPTIMIZER_STATE_CHANGE,
                    event_data={
                        'action_type': ACTION_ACTIVATE,
                        'requested_power': 500,
                        'current_power': 0,
                        'entity_id': 'input_boolean.fake_device_a'
                    }),
                call(
                    event_type=EVENT_TYPE_SOLAR_OPTIMIZER_CHANGE_POWER,
                    event_data={
                        'action_type': ACTION_CHANGE_POWER,
                        'requested_power': 500,
                        'current_power': 0,
                        'entity_id': 'input_number.fake_amps_number'
                    })
            ],
            any_order=True,
        )

    # 2. one minute later, the device should be still waiting. The power should not change
    now = now + timedelta(minutes=1)
    device._set_now(now)
    # -300 is -800 of the production - 500 of the consumption
    side_effects.add_or_update_side_effect("sensor.fake_power_production", State("sensor.fake_power_production", 800))
    side_effects.add_or_update_side_effect("sensor.fake_power_consumption", State("sensor.fake_power_consumption", -300))
    # we add the Get state side effect for the input_number fake Amps
    side_effects.add_or_update_side_effect("input_number.fake_amps_number", State("input_number.fake_amps_number", 50))

    # fmt:off
    with patch("homeassistant.core.StateMachine.get", side_effect=side_effects.get_side_effects()), \
         patch("custom_components.solar_optimizer.managed_device.ManagedDevice.is_active", new_callable=PropertyMock, return_value=True), \
         patch("homeassistant.core.EventBus.fire") as mock_fire:
    # fmt:on
        assert device.check_usable() is False
        calculated_data = await coordinator._async_update_data()
        await hass.async_block_till_done()

        assert fake_device_a.state == STATE_ON

        # No change
        assert calculated_data["total_power"] == 500
        assert calculated_data["power_device_a"].is_waiting is True
        assert (calculated_data["power_device_a"].next_date_available.astimezone(tz) - now).total_seconds() >= 540  # 9 minutes
        assert (calculated_data["power_device_a"].next_date_available_power.astimezone(tz) - now).total_seconds() >= 60

        assert fake_device_a.state == STATE_ON
        assert fake_amps_number.state == 50

        assert mock_fire.call_count == 0

    # 3. one minute later, the power can be changed
    now = now + timedelta(minutes=1)
    device._set_now(now)

    # fmt:off
    with patch("homeassistant.core.StateMachine.get", side_effect=side_effects.get_side_effects()), \
         patch("custom_components.solar_optimizer.managed_device.ManagedDevice.is_active", new_callable=PropertyMock, return_value=True), \
         patch("homeassistant.core.EventBus.fire") as mock_fire:
    # fmt:on
        # the device is waiting but the power can be changed (usable = True)
        assert device.check_usable() is True
        assert device.is_waiting is True

        calculated_data = await coordinator._async_update_data()
        await hass.async_block_till_done()

        assert fake_device_a.state == STATE_ON

        # Changes !
        assert calculated_data["total_power"] == 800
        assert calculated_data["power_device_a"].is_waiting is True
        assert (calculated_data["power_device_a"].next_date_available.astimezone(tz) - now).total_seconds() >= 480  # 8 minutes
        assert (calculated_data["power_device_a"].next_date_available_power.astimezone(tz) - now).total_seconds() >= 120 # 2 minutes now cause the power changed

        assert fake_device_a.state == STATE_ON
        assert fake_amps_number.state == 80

        # check hass.bus.fire has been called
        assert mock_fire.call_count == 1
        mock_fire.assert_has_calls(
            [
                call(
                    event_type=EVENT_TYPE_SOLAR_OPTIMIZER_CHANGE_POWER,
                    event_data={
                        'action_type': ACTION_CHANGE_POWER,
                        'requested_power': 800,
                        'current_power': 500,
                        'entity_id': 'input_number.fake_amps_number'
                    })
            ],
            any_order=True,
        )

    # 4. one minute later, the device and power are waiting
    now = now + timedelta(minutes=1)
    device._set_now(now)
    # slow down the production
    side_effects.add_or_update_side_effect("sensor.fake_power_production", State("sensor.fake_power_production", 0))
    side_effects.add_or_update_side_effect("sensor.fake_power_consumption", State("sensor.fake_power_consumption", 800))
    # we add the Get state side effect for the input_number fake Amps
    side_effects.add_or_update_side_effect("input_number.fake_amps_number", State("input_number.fake_amps_number", 80))

    # fmt:off
    with patch("homeassistant.core.StateMachine.get", side_effect=side_effects.get_side_effects()), \
         patch("custom_components.solar_optimizer.managed_device.ManagedDevice.is_active", new_callable=PropertyMock, return_value=True), \
         patch("homeassistant.core.EventBus.fire") as mock_fire:
    # fmt:on
        # the device is waiting but the power can be changed (usable = True)
        assert device.check_usable() is False
        assert device.is_waiting is True

        calculated_data = await coordinator._async_update_data()
        await hass.async_block_till_done()

        # Surplus-only control now overrides the minimum run when solar is gone.
        assert fake_device_a.state == STATE_OFF
        assert calculated_data["total_power"] == 0
        assert calculated_data["best_solution"][0]["decision_reason"] == "insufficient_surplus"
        assert device.is_waiting is True
        assert (device.next_date_available.astimezone(tz) - now).total_seconds() >= 300
        assert fake_amps_number.state == 80  # Stop, not an early power-setpoint command.
        mock_fire.assert_has_calls([
            call(event_type=EVENT_TYPE_SOLAR_OPTIMIZER_STATE_CHANGE, event_data={
                "action_type": ACTION_DEACTIVATE, "requested_power": 0,
                "current_power": 800, "entity_id": "input_boolean.fake_device_a",
            })
        ])

    # Solar returns immediately, but the minimum-off interval still blocks restart.
    side_effects.add_or_update_side_effect("sensor.fake_power_consumption", State("sensor.fake_power_consumption", -1500))
    side_effects.add_or_update_side_effect("sensor.fake_power_production", State("sensor.fake_power_production", 2000))
    now += timedelta(minutes=1)
    device._set_now(now)
    with patch("homeassistant.core.StateMachine.get", side_effect=side_effects.get_side_effects()), patch(
        "custom_components.solar_optimizer.managed_device.ManagedDevice.is_active",
        new_callable=PropertyMock, return_value=False
    ):
        calculated_data = await coordinator._async_update_data()
        assert calculated_data["total_power"] == 0
        assert fake_device_a.state == STATE_OFF


async def test_light_power_device(
    hass: HomeAssistant,
    init_solar_optimizer_central_config,
):
    """Testing light device"""

    entry_a = MockConfigEntry(
        domain=DOMAIN,
        title="Equipement A",
        unique_id="eqtAUniqueId",
        data={
            CONF_NAME: "Equipement A",
            CONF_DEVICE_TYPE: CONF_POWERED_DEVICE,
            CONF_ENTITY_ID: "light.fake_device_a",
            CONF_POWER_MAX: 1000,
            CONF_CHECK_USABLE_TEMPLATE: "{{ True }}",
            CONF_DURATION_MIN: 0.3,
            CONF_DURATION_STOP_MIN: 0.1,
            CONF_ACTION_MODE: CONF_ACTION_MODE_ACTION,
            CONF_ACTIVATION_SERVICE: "light/turn_on",
            CONF_DEACTIVATION_SERVICE: "light/turn_off",
            CONF_POWER_MIN: 100,
            CONF_POWER_STEP: 4,
            CONF_POWER_ENTITY_ID: "light.fake_device_a",
            CONF_DURATION_POWER_MIN: 0.25,
            CONF_CHANGE_POWER_SERVICE: "light/turn_on/brightness",
            CONF_CONVERT_POWER_DIVIDE_FACTOR: 4,
        },
    )

    device = await create_managed_device(
        hass,
        entry_a,
        "equipement_a",
    )
    assert device is not None
    assert device.name == "Equipement A"

    #
    # Disable the device by simulating a call into the switch enable sensor
    #
    enable_switch = search_entity(hass, "switch.enable_solar_optimizer_equipement_a", SWITCH_DOMAIN)
    assert enable_switch is not None

    device_switch = search_entity(hass, "switch.solar_optimizer_equipement_a", SWITCH_DOMAIN)
    assert device_switch is not None

    # try to activate the device directly
    # fmt: off
    with patch("homeassistant.core.ServiceRegistry.async_call") as mock_service_call:
    # fmt: on
        await device.activate()

        mock_service_call.assert_has_calls(
                [
                    call.service_call(
                        "light",
                        "turn_on",
                        service_data= {},
                        target= {
                            "entity_id": "light.fake_device_a",
                        },
                    ),
                ]
            )

    # try to change the power of the device directly to max power
    # fmt: off
    with patch("homeassistant.core.ServiceRegistry.async_call") as mock_service_call:
    # fmt: on
        await device.change_requested_power(1000)

        mock_service_call.assert_has_calls(
                [
                    call.call(
                        "light",
                        "turn_on",
                        service_data= { 'brightness': 250 },  # 1000 / 4 = 250
                        target= {
                            "entity_id": "light.fake_device_a",
                        },
                    ),
                ]
            )

    # try to change the power of the device directly to 50% power
    # fmt: off
    with patch("homeassistant.core.ServiceRegistry.async_call") as mock_service_call:
    # fmt: on
        await device.change_requested_power(500)

        mock_service_call.assert_has_calls(
                [
                    call.call(
                        "light",
                        "turn_on",
                        service_data= { 'brightness': 125 },  # 500 / 4 = 125
                        target= {
                            "entity_id": "light.fake_device_a",
                        },
                    ),
                ]
            )

    # try to de-activate the device directly
    # fmt: off
    with patch("homeassistant.core.ServiceRegistry.async_call") as mock_service_call:
    # fmt: on
        await device.deactivate()

        mock_service_call.assert_has_calls(
                [
                    call.service_call(
                        "light",
                        "turn_off",
                        service_data= {},
                        target= {
                            "entity_id": "light.fake_device_a",
                        },
                    ),
                ]
            )


async def test_fan_power_device(
    hass: HomeAssistant,
    init_solar_optimizer_central_config,
):
    """Testing light device"""

    entry_a = MockConfigEntry(
        domain=DOMAIN,
        title="Equipement A",
        unique_id="eqtAUniqueId",
        data={
            CONF_NAME: "Equipement A",
            CONF_DEVICE_TYPE: CONF_POWERED_DEVICE,
            CONF_ENTITY_ID: "fan.fake_device_a",
            CONF_POWER_MAX: 1000,
            CONF_CHECK_USABLE_TEMPLATE: "{{ True }}",
            CONF_DURATION_MIN: 0.3,
            CONF_DURATION_STOP_MIN: 0.1,
            CONF_ACTION_MODE: CONF_ACTION_MODE_ACTION,
            CONF_ACTIVATION_SERVICE: "fan/turn_on",
            CONF_DEACTIVATION_SERVICE: "fan/turn_off",
            CONF_POWER_MIN: 100,
            CONF_POWER_STEP: 10,
            CONF_POWER_ENTITY_ID: "fan.fake_device_a",
            CONF_DURATION_POWER_MIN: 0.25,
            CONF_CHANGE_POWER_SERVICE: "fan/turn_on/percentage",
            CONF_CONVERT_POWER_DIVIDE_FACTOR: 10,
        },
    )

    device = await create_managed_device(
        hass,
        entry_a,
        "equipement_a",
    )
    assert device is not None
    assert device.name == "Equipement A"

    #
    # Disable the device by simulating a call into the switch enable sensor
    #
    enable_switch = search_entity(hass, "switch.enable_solar_optimizer_equipement_a", SWITCH_DOMAIN)
    assert enable_switch is not None

    device_switch = search_entity(hass, "switch.solar_optimizer_equipement_a", SWITCH_DOMAIN)
    assert device_switch is not None

    # try to activate the device directly
    # fmt: off
    with patch("homeassistant.core.ServiceRegistry.async_call") as mock_service_call:
    # fmt: on
        await device.activate()

        mock_service_call.assert_has_calls(
                [
                    call.service_call(
                        "fan",
                        "turn_on",
                        service_data= {},
                        target= {
                            "entity_id": "fan.fake_device_a",
                        },
                    ),
                ]
            )

    # try to change the power of the device directly to max power
    # fmt: off
    with patch("homeassistant.core.ServiceRegistry.async_call") as mock_service_call:
    # fmt: on
        await device.change_requested_power(1000)

        mock_service_call.assert_has_calls(
                [
                    call.call(
                        "fan",
                        "turn_on",
                        service_data= { 'percentage': 100 },  # 1000 / 10 = 100%
                        target= {
                            "entity_id": "fan.fake_device_a",
                        },
                    ),
                ]
            )

    # try to change the power of the device directly to 50% power
    # fmt: off
    with patch("homeassistant.core.ServiceRegistry.async_call") as mock_service_call:
    # fmt: on
        await device.change_requested_power(500)

        mock_service_call.assert_has_calls(
                [
                    call.call(
                        "fan",
                        "turn_on",
                        service_data= { 'percentage': 50 },  # 500 / 10 = 50%
                        target= {
                            "entity_id": "fan.fake_device_a",
                        },
                    ),
                ]
            )

    # try to de-activate the device directly
    # fmt: off
    with patch("homeassistant.core.ServiceRegistry.async_call") as mock_service_call:
    # fmt: on
        await device.deactivate()

        mock_service_call.assert_has_calls(
                [
                    call.service_call(
                        "fan",
                        "turn_off",
                        service_data= {},
                        target= {
                            "entity_id": "fan.fake_device_a",
                        },
                    ),
                ]
            )

import tempfile
from unittest.mock import MagicMock

from srlobo_mqtt_bridge.commands import CommandHandler
from srlobo_mqtt_bridge.court_signal import CourtSignalPublisher
from srlobo_mqtt_bridge.config import AddonOptions, BootstrapConfig, CourtEntity, DoorEntity, MqttConfig
from srlobo_mqtt_bridge.entity_registry import EntityRegistry
from srlobo_mqtt_bridge.ha_client import HomeAssistantError


def _options() -> AddonOptions:
    return AddonOptions(
        srlobo_token="t",
        srlobo_api_url="https://srlobo.example",
        bootstrap_path="/x",
        log_level="info",
        mode_select_entity_template="input_select.modo_pista_{n}",
        calibration_trigger_entity_template="input_button.calibrar_pista_{n}",
    )


def _registry() -> EntityRegistry:
    bootstrap = BootstrapConfig(
        installation_id="club_1",
        source_system=None,
        mqtt=MqttConfig(broker="b", base_topic="srlobo/club_1"),
        courts=[CourtEntity(index=0)],
        doors=[DoorEntity(index=0, entity_id="lock.puerta_1")],
    )
    return EntityRegistry(bootstrap)


def _handler(ha=None):
    ha = ha or MagicMock()
    mqtt = MagicMock()
    signals = CourtSignalPublisher(ha, _registry(), "club_1", path=f"{tempfile.mkdtemp()}/signals.json")
    handler = CommandHandler(ha, mqtt, _registry(), _options(), signals)
    return handler, ha, mqtt


def _light_calls(ha):
    return [c for c in ha.call_service.call_args_list if c.args[0] == "light"]


def test_court_on_with_brightness_sets_signal_never_the_light_and_acks_executed():
    handler, ha, mqtt = _handler()

    handler.handle_court_command(0, {"command_id": "cmd-1", "state": "on", "brightness_pct": 70})

    entity_id, state, attributes = ha.set_state.call_args.args
    assert entity_id == "binary_sensor.pista_1"
    assert state == "on"
    assert attributes["brightness"] == 70
    assert _light_calls(ha) == []
    ack_payload = mqtt.publish_court_ack.call_args[0][1]
    assert ack_payload["command_id"] == "cmd-1"
    assert ack_payload["status"] == "executed"
    assert ack_payload["error"] is None


def test_court_off_sets_signal_off_with_zero_brightness():
    handler, ha, mqtt = _handler()
    handler.handle_court_command(0, {"command_id": "cmd-2", "state": "off"})
    entity_id, state, attributes = ha.set_state.call_args.args
    assert (entity_id, state, attributes["brightness"]) == ("binary_sensor.pista_1", "off", 0)
    assert _light_calls(ha) == []


def test_mode_only_command_does_not_touch_the_signal():
    handler, ha, mqtt = _handler()
    handler.handle_court_command(0, {"command_id": "cmd-2b", "mode": "AUTO"})
    ha.set_state.assert_not_called()


def test_court_mode_failure_does_not_fail_the_whole_command():
    """Mode-select entity name is unconfirmed against the real HA blueprint.
    A failure there must not undo the on/off/brightness part of the same
    command, and must still ack 'executed' since the light control succeeded."""
    ha = MagicMock()

    def call_service(domain, service, entity_id, data=None):
        if domain == "input_select":
            raise HomeAssistantError("entity not found")

    ha.call_service.side_effect = call_service
    handler, ha, mqtt = _handler(ha)

    handler.handle_court_command(0, {"command_id": "cmd-3", "state": "on", "mode": "MANUAL"})

    ack_payload = mqtt.publish_court_ack.call_args[0][1]
    assert ack_payload["status"] == "executed"


def test_lux_loop_activation_seeds_lux_target():
    handler, ha, mqtt = _handler()
    handler.handle_court_command(
        0, {"command_id": "cmd-lux-1", "mode": "LUX_LOOP", "lux_target": 320}
    )
    ha.call_service.assert_any_call(
        "input_number", "set_value", "input_number.referencia_lux_pista_1", {"value": 320}
    )


def test_mode_only_command_without_lux_target_does_not_touch_lux_reference():
    handler, ha, mqtt = _handler()
    handler.handle_court_command(0, {"command_id": "cmd-lux-2", "mode": "AUTO"})
    for call in ha.call_service.call_args_list:
        assert call.args[0] != "input_number"


def test_lux_target_seed_failure_does_not_fail_the_whole_command():
    ha = MagicMock()

    def call_service(domain, service, entity_id, data=None):
        if domain == "input_number":
            raise HomeAssistantError("entity not found")

    ha.call_service.side_effect = call_service
    handler, ha, mqtt = _handler(ha)

    handler.handle_court_command(
        0, {"command_id": "cmd-lux-3", "mode": "LUX_LOOP", "lux_target": 320}
    )

    ack_payload = mqtt.publish_court_ack.call_args[0][1]
    assert ack_payload["status"] == "executed"


def test_calibrate_action_calls_input_button_press():
    handler, ha, mqtt = _handler()
    handler.handle_court_command(
        0, {"command_id": "cmd-4", "action": "calibrate", "calibration_power_pct": 65}
    )
    ha.call_service.assert_called_with("input_button", "press", "input_button.calibrar_pista_1", {"power_pct": 65})


def test_court_command_for_unknown_index_is_ignored():
    handler, ha, mqtt = _handler()
    handler.handle_court_command(99, {"command_id": "cmd-5", "state": "on"})
    ha.call_service.assert_not_called()
    ha.set_state.assert_not_called()
    mqtt.publish_court_ack.assert_not_called()


def test_court_command_ha_error_acks_failed():
    ha = MagicMock()
    ha.set_state.side_effect = HomeAssistantError("shelly not responding")
    handler, ha, mqtt = _handler(ha)

    handler.handle_court_command(0, {"command_id": "cmd-6", "state": "on"})

    ack_payload = mqtt.publish_court_ack.call_args[0][1]
    assert ack_payload["status"] == "failed"
    assert "shelly not responding" in ack_payload["error"]


def test_door_unlock_calls_lock_unlock():
    handler, ha, mqtt = _handler()
    handler.handle_door_command(0, {"command_id": "cmd-7", "action": "unlock"})
    ha.call_service.assert_called_with("lock", "unlock", "lock.puerta_1")


def test_door_command_never_publishes_an_ack():
    """ADR-002 §5.2 only defines an ack topic for courts."""
    handler, ha, mqtt = _handler()
    handler.handle_door_command(0, {"command_id": "cmd-8", "action": "open"})
    mqtt.publish_court_ack.assert_not_called()


def test_door_command_unknown_action_is_ignored():
    handler, ha, mqtt = _handler()
    handler.handle_door_command(0, {"command_id": "cmd-9", "action": "explode"})
    ha.call_service.assert_not_called()


def test_door_command_for_unconfigured_door_does_not_crash():
    handler, ha, mqtt = _handler()
    handler.handle_door_command(99, {"command_id": "cmd-10", "action": "open"})
    ha.call_service.assert_not_called()


def test_last_door_action_records_executed_action():
    handler, ha, mqtt = _handler()
    assert handler.last_door_action(0) is None  # nothing executed yet
    handler.handle_door_command(0, {"command_id": "cmd-11", "action": "unlock"})
    assert handler.last_door_action(0) == "unlock"
    handler.handle_door_command(0, {"command_id": "cmd-12", "action": "close"})
    assert handler.last_door_action(0) == "close"  # most recent wins


def test_last_door_action_not_recorded_on_ha_failure():
    handler, ha, mqtt = _handler()
    ha.call_service.side_effect = HomeAssistantError("lock unreachable")
    handler.handle_door_command(0, {"command_id": "cmd-13", "action": "open"})
    assert handler.last_door_action(0) is None


def test_last_door_action_not_recorded_for_unknown_action():
    handler, ha, mqtt = _handler()
    handler.handle_door_command(0, {"command_id": "cmd-14", "action": "explode"})
    assert handler.last_door_action(0) is None

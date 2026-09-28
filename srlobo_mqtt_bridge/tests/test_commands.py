from unittest.mock import MagicMock

from srlobo_mqtt_bridge.commands import CommandHandler
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
        courts=[CourtEntity(index=1)],
        doors=[DoorEntity(index=1, entity_id="lock.puerta_1")],
    )
    return EntityRegistry(bootstrap)


def _handler(ha=None):
    ha = ha or MagicMock()
    mqtt = MagicMock()
    handler = CommandHandler(ha, mqtt, _registry(), _options())
    return handler, ha, mqtt


def test_court_on_with_brightness_calls_light_turn_on_and_acks_executed():
    handler, ha, mqtt = _handler()

    handler.handle_court_command(1, {"command_id": "cmd-1", "state": "on", "brightness_pct": 70})

    ha.call_service.assert_any_call("light", "turn_on", "light.luces_padel_1", {"brightness_pct": 70})
    ack_payload = mqtt.publish_court_ack.call_args[0][1]
    assert ack_payload["command_id"] == "cmd-1"
    assert ack_payload["status"] == "executed"
    assert ack_payload["error"] is None


def test_court_off_calls_light_turn_off():
    handler, ha, mqtt = _handler()
    handler.handle_court_command(1, {"command_id": "cmd-2", "state": "off"})
    ha.call_service.assert_any_call("light", "turn_off", "light.luces_padel_1")


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

    handler.handle_court_command(1, {"command_id": "cmd-3", "state": "on", "mode": "MANUAL"})

    ack_payload = mqtt.publish_court_ack.call_args[0][1]
    assert ack_payload["status"] == "executed"


def test_calibrate_action_calls_input_button_press():
    handler, ha, mqtt = _handler()
    handler.handle_court_command(
        1, {"command_id": "cmd-4", "action": "calibrate", "calibration_power_pct": 65}
    )
    ha.call_service.assert_called_with("input_button", "press", "input_button.calibrar_pista_1", {"power_pct": 65})


def test_court_command_for_unknown_index_is_ignored():
    handler, ha, mqtt = _handler()
    handler.handle_court_command(99, {"command_id": "cmd-5", "state": "on"})
    ha.call_service.assert_not_called()
    mqtt.publish_court_ack.assert_not_called()


def test_court_command_ha_error_acks_failed():
    ha = MagicMock()
    ha.call_service.side_effect = HomeAssistantError("shelly not responding")
    handler, ha, mqtt = _handler(ha)

    handler.handle_court_command(1, {"command_id": "cmd-6", "state": "on"})

    ack_payload = mqtt.publish_court_ack.call_args[0][1]
    assert ack_payload["status"] == "failed"
    assert "shelly not responding" in ack_payload["error"]


def test_door_unlock_calls_lock_unlock():
    handler, ha, mqtt = _handler()
    handler.handle_door_command(1, {"command_id": "cmd-7", "action": "unlock"})
    ha.call_service.assert_called_with("lock", "unlock", "lock.puerta_1")


def test_door_command_never_publishes_an_ack():
    """ADR-002 §5.2 only defines an ack topic for courts."""
    handler, ha, mqtt = _handler()
    handler.handle_door_command(1, {"command_id": "cmd-8", "action": "open"})
    mqtt.publish_court_ack.assert_not_called()


def test_door_command_unknown_action_is_ignored():
    handler, ha, mqtt = _handler()
    handler.handle_door_command(1, {"command_id": "cmd-9", "action": "explode"})
    ha.call_service.assert_not_called()


def test_door_command_for_unconfigured_door_does_not_crash():
    handler, ha, mqtt = _handler()
    handler.handle_door_command(99, {"command_id": "cmd-10", "action": "open"})
    ha.call_service.assert_not_called()

from unittest.mock import MagicMock

from srlobo_mqtt_bridge.config import MqttConfig
from srlobo_mqtt_bridge.mqtt_client import BridgeMqttClient


def _client() -> BridgeMqttClient:
    client = BridgeMqttClient(MqttConfig(broker="b", base_topic="srlobo/club_1", tls=False), "club_1")
    client.on_court_command(MagicMock())
    client.on_door_command(MagicMock())
    client.on_venue_config(MagicMock())
    client.on_schedule(MagicMock())
    return client


def test_heartbeat_and_commands_count_as_cloud_signals():
    client = _client()
    signal = MagicMock()
    client.on_cloud_signal(signal)

    client._dispatch("srlobo/club_1/cloud/heartbeat", {"timestamp": "2026-08-19T13:30:00Z"})
    client._dispatch("srlobo/club_1/courts/0/command", {"state": "on"})
    client._dispatch("srlobo/club_1/doors/0/command", {"action": "open"})

    assert signal.call_count == 3


def test_retained_topics_never_count_as_cloud_signals():
    client = _client()
    signal = MagicMock()
    client.on_cloud_signal(signal)

    client._dispatch("srlobo/club_1/venue/config", {"schema_version": 1})
    client._dispatch("srlobo/club_1/schedule", {"schema_version": 1})

    signal.assert_not_called()
    client._schedule_handler.assert_called_once_with({"schema_version": 1})


def test_subscribes_to_schedule_and_heartbeat():
    client = _client()
    paho = MagicMock()
    client._on_connect(paho, None, {}, 0)
    topics = [call.args[0] for call in paho.subscribe.call_args_list]
    assert "srlobo/club_1/schedule" in topics
    assert "srlobo/club_1/cloud/heartbeat" in topics

from unittest.mock import MagicMock, patch

import pytest

from srlobo_mqtt_bridge.bootstrap import BootstrapError, SrLoboBootstrapClient

VALID_PAYLOAD = {
    "installation_id": "club_109",
    "club": {"uuid": "club_109", "source_system": "playtomic"},
    "mqtt": {
        "broker": "mqtt.srlobo.example",
        "port": 8883,
        "tls": True,
        "username": "mqtt-user",
        "password": "mqtt-password",
        "client_id": "ha-addon-club-109",
        "base_topic": "srlobo/club_109",
    },
    "courts": [{"index": 1, "source_id": "72", "source_system": "playtomic"}],
    "doors": [{"index": 1, "entity_id": "lock.puerta_1", "source_id": "41"}],
}


def _client() -> SrLoboBootstrapClient:
    return SrLoboBootstrapClient(
        api_url="https://srlobo.example", bootstrap_path="/api/homeassistant/bootstrap", token="secret-token"
    )


def _mock_response(json_body):
    response = MagicMock()
    response.json.return_value = json_body
    response.raise_for_status.return_value = None
    return response


def test_fetch_parses_valid_payload():
    with patch("srlobo_mqtt_bridge.bootstrap.requests.get", return_value=_mock_response(VALID_PAYLOAD)):
        config = _client().fetch()

    assert config.installation_id == "club_109"
    assert config.mqtt.broker == "mqtt.srlobo.example"
    assert config.mqtt.username == "mqtt-user"
    assert config.mqtt.password == "mqtt-password"
    assert len(config.courts) == 1
    assert config.courts[0].index == 1
    assert len(config.doors) == 1
    assert config.doors[0].entity_id == "lock.puerta_1"


def test_fetch_never_logs_the_bearer_token(caplog):
    with patch("srlobo_mqtt_bridge.bootstrap.requests.get", return_value=_mock_response(VALID_PAYLOAD)):
        _client().fetch()
    assert "secret-token" not in caplog.text
    assert "mqtt-password" not in caplog.text


def test_missing_mqtt_field_raises_bootstrap_error():
    bad_payload = {**VALID_PAYLOAD, "mqtt": {"broker": "mqtt.example"}}  # missing base_topic
    with patch("srlobo_mqtt_bridge.bootstrap.requests.get", return_value=_mock_response(bad_payload)):
        with pytest.raises(BootstrapError):
            _client().fetch()


def test_retries_then_raises_after_max_attempts():
    import requests

    with patch(
        "srlobo_mqtt_bridge.bootstrap.requests.get", side_effect=requests.ConnectionError("boom")
    ), patch("srlobo_mqtt_bridge.bootstrap.time.sleep"):
        with pytest.raises(BootstrapError):
            _client().fetch()

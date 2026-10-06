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


def _cached_client(tmp_path, token="secret-token") -> SrLoboBootstrapClient:
    return SrLoboBootstrapClient(
        api_url="https://srlobo.example",
        bootstrap_path="/api/homeassistant/bootstrap",
        token=token,
        cache_path=str(tmp_path / "bootstrap_cache.json"),
    )


def _offline_fetch(client):
    import requests

    with patch(
        "srlobo_mqtt_bridge.bootstrap.requests.get", side_effect=requests.ConnectionError("offline")
    ), patch("srlobo_mqtt_bridge.bootstrap.time.sleep"):
        return client.fetch()


def test_offline_startup_uses_cached_response(tmp_path):
    with patch("srlobo_mqtt_bridge.bootstrap.requests.get", return_value=_mock_response(VALID_PAYLOAD)):
        _cached_client(tmp_path).fetch()

    config = _offline_fetch(_cached_client(tmp_path))
    assert config.installation_id == "club_109"
    assert config.mqtt.broker == "mqtt.srlobo.example"


def test_cache_never_stores_the_token_itself(tmp_path):
    with patch("srlobo_mqtt_bridge.bootstrap.requests.get", return_value=_mock_response(VALID_PAYLOAD)):
        _cached_client(tmp_path).fetch()
    assert "secret-token" not in (tmp_path / "bootstrap_cache.json").read_text()


def test_cache_from_another_token_is_ignored(tmp_path):
    # clubs are cloned from the master backup, cache and all
    with patch("srlobo_mqtt_bridge.bootstrap.requests.get", return_value=_mock_response(VALID_PAYLOAD)):
        _cached_client(tmp_path, token="master-token").fetch()

    with pytest.raises(BootstrapError):
        _offline_fetch(_cached_client(tmp_path, token="clone-token"))


def test_unauthorized_is_not_retried_and_drops_the_cache(tmp_path):
    from srlobo_mqtt_bridge.bootstrap import BootstrapUnauthorizedError

    with patch("srlobo_mqtt_bridge.bootstrap.requests.get", return_value=_mock_response(VALID_PAYLOAD)):
        _cached_client(tmp_path).fetch()

    rejected = MagicMock()
    rejected.status_code = 401
    with patch("srlobo_mqtt_bridge.bootstrap.requests.get", return_value=rejected) as get, patch(
        "srlobo_mqtt_bridge.bootstrap.time.sleep"
    ):
        with pytest.raises(BootstrapUnauthorizedError):
            _cached_client(tmp_path).fetch()
    assert get.call_count == 1
    assert not (tmp_path / "bootstrap_cache.json").exists()

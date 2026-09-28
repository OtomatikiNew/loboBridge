from unittest.mock import MagicMock

from srlobo_mqtt_bridge.config import AddonOptions
from srlobo_mqtt_bridge.discovery import CourtDiscovery, DoorDiscovery, MemberDevice
from srlobo_mqtt_bridge.telemetry import StateCache, TelemetryPublisher, _member_entities_dict


def _options() -> AddonOptions:
    return AddonOptions(
        srlobo_token="t",
        srlobo_api_url="https://srlobo.example",
        bootstrap_path="/x",
        log_level="info",
    )


def test_state_cache_seed_and_update():
    cache = StateCache()
    cache.seed([{"entity_id": "light.w29", "state": "on"}])
    assert cache.get("light.w29")["state"] == "on"
    cache.update("light.w29", {"state": "off"})
    assert cache.get("light.w29")["state"] == "off"
    assert cache.get("light.unknown") is None


def test_member_entities_dict_only_includes_cached_entities():
    cache = StateCache()
    cache.seed([{"entity_id": "sensor.w29_potencia", "state": "450"}])
    result = _member_entities_dict(cache, ["sensor.w29_potencia", "sensor.w29_energia"])
    assert result == {"sensor.w29_potencia": "450"}  # w29_energia never seen, correctly absent


def test_build_court_payload_unfiltered_passthrough():
    ha = MagicMock()
    ha.get_state.return_value = None  # no calibration entity configured yet
    mqtt = MagicMock()
    publisher = TelemetryPublisher(mqtt, ha, _options())
    publisher._cache.seed(
        [
            {"entity_id": "light.luces_padel_1", "state": "on", "attributes": {"brightness": 191}},
            {"entity_id": "light.w29", "state": "on"},
            {"entity_id": "sensor.w29_potencia", "state": "450"},
        ]
    )

    court = CourtDiscovery(
        index=1,
        helper_entity_id="light.luces_padel_1",
        devices=[MemberDevice(member_entity_id="light.w29", entity_ids=["light.w29", "sensor.w29_potencia"])],
    )

    payload = publisher._build_court_payload(court)

    assert payload["court_index"] == 1
    assert payload["helper"]["state"] == "on"
    assert payload["helper"]["brightness_pct"] == 75  # 191/255*100 rounded
    assert payload["devices"] == [
        {"member_entity_id": "light.w29", "entities": {"light.w29": "on", "sensor.w29_potencia": "450"}}
    ]
    assert "reference_lux" not in payload  # no calibration entity found -> field omitted, not fabricated


def test_build_court_payload_includes_calibration_when_present():
    ha = MagicMock()
    ha.get_state.return_value = {
        "state": "320",
        "attributes": {"reference_power_pct": 65, "calibrated_at": "2026-09-17T10:02:15Z"},
        "last_changed": "2026-09-17T10:02:15Z",
    }
    mqtt = MagicMock()
    publisher = TelemetryPublisher(mqtt, ha, _options())
    publisher._cache.seed([{"entity_id": "light.luces_padel_1", "state": "on"}])

    court = CourtDiscovery(index=1, helper_entity_id="light.luces_padel_1", devices=[])
    payload = publisher._build_court_payload(court)

    assert payload["reference_lux"] == 320
    assert payload["reference_power_pct"] == 65
    assert payload["calibrated_at"] == "2026-09-17T10:02:15Z"


def test_build_door_payload():
    ha = MagicMock()
    mqtt = MagicMock()
    publisher = TelemetryPublisher(mqtt, ha, _options())
    publisher._cache.seed(
        [
            {"entity_id": "lock.puerta_1", "state": "locked"},
            {"entity_id": "sensor.puerta_1_battery", "state": "88"},
        ]
    )

    door = DoorDiscovery(
        index=1,
        lock_entity_id="lock.puerta_1",
        devices=[
            MemberDevice(
                member_entity_id="lock.puerta_1", entity_ids=["lock.puerta_1", "sensor.puerta_1_battery"]
            )
        ],
    )

    payload = publisher._build_door_payload(door)

    assert payload["door_index"] == 1
    assert payload["lock"]["state"] == "locked"
    assert payload["devices"][0]["entities"]["sensor.puerta_1_battery"] == "88"


def test_on_state_changed_ignores_entities_not_owned():
    """An event for an entity this bridge doesn't own must never trigger a
    publish. Runtime half of the entity-ownership principle."""
    from srlobo_mqtt_bridge.discovery import DiscoveryState

    ha = MagicMock()
    mqtt = MagicMock()
    publisher = TelemetryPublisher(mqtt, ha, _options())
    publisher.on_rediscover(DiscoveryState())  # empty, nothing owned
    mqtt.reset_mock()

    publisher.on_state_changed(
        {"entity_id": "binary_sensor.unrelated_solar_helper", "new_state": {"state": "on"}}
    )

    mqtt.publish_court_telemetry.assert_not_called()
    mqtt.publish_door_telemetry.assert_not_called()

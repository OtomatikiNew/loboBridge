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
    publisher = TelemetryPublisher(mqtt, ha, _options(), MagicMock())
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
    publisher = TelemetryPublisher(mqtt, ha, _options(), MagicMock())
    publisher._cache.seed([{"entity_id": "light.luces_padel_1", "state": "on"}])

    court = CourtDiscovery(index=0, helper_entity_id="light.luces_padel_1", devices=[])
    payload = publisher._build_court_payload(court)

    ha.get_state.assert_called_with("input_number.referencia_lux_pista_1")
    assert payload["reference_lux"] == 320
    assert payload["reference_power_pct"] == 65
    assert payload["calibrated_at"] == "2026-09-17T10:02:15Z"


def test_build_door_payload():
    ha = MagicMock()
    mqtt = MagicMock()
    publisher = TelemetryPublisher(mqtt, ha, _options(), MagicMock())
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


def test_build_court_state_payload_derives_normalized_fields():
    mqtt = MagicMock()
    publisher = TelemetryPublisher(mqtt, MagicMock(), _options(), MagicMock())
    telemetry_payload = {
        "helper": {"state": "on", "brightness_pct": 75},
        "devices": [
            {
                "member_entity_id": "light.w29",
                "entities": {
                    "light.w29": "on",
                    "sensor.w29_potencia": "450",
                    "sensor.w29_wifi_rssi": "-62",
                },
            },
            {
                "member_entity_id": "light.w30",
                "entities": {"sensor.w30_potencia": "448", "sensor.w30_wifi_rssi": "-70"},
            },
        ],
    }

    state = publisher._build_court_state_payload(telemetry_payload)

    assert state["light_on"] is True
    assert state["brightness_pct"] == 75
    assert state["power_w"] == 898  # 450 + 448
    assert state["wifi_rssi"] == -70  # worst (lowest) of the two
    assert state["shelly_online"] is True
    # no lux, AP or mode entities in this payload
    assert state["lux_measured"] is None
    assert state["wifi_ap"] is None
    assert state["mode"] is None


def test_build_court_state_payload_detects_unavailable_device():
    mqtt = MagicMock()
    publisher = TelemetryPublisher(mqtt, MagicMock(), _options(), MagicMock())
    telemetry_payload = {
        "helper": {"state": "off"},
        "devices": [{"member_entity_id": "light.w29", "entities": {"light.w29": "unavailable"}}],
    }

    state = publisher._build_court_state_payload(telemetry_payload)

    assert state["shelly_online"] is False


def test_build_door_state_payload_includes_last_action_from_commands():
    commands = MagicMock()
    commands.last_door_action.return_value = "unlock"
    mqtt = MagicMock()
    publisher = TelemetryPublisher(mqtt, MagicMock(), _options(), commands)
    telemetry_payload = {"lock": {"state": "locked"}, "devices": []}

    state = publisher._build_door_state_payload(1, telemetry_payload)

    assert state["locked"] is True
    assert state["online"] is True
    assert state["last_action"] == "unlock"
    commands.last_door_action.assert_called_once_with(1)


def test_build_door_state_payload_last_action_none_when_nothing_executed_yet():
    commands = MagicMock()
    commands.last_door_action.return_value = None
    mqtt = MagicMock()
    publisher = TelemetryPublisher(mqtt, MagicMock(), _options(), commands)

    state = publisher._build_door_state_payload(1, {"lock": {"state": "unlocked"}, "devices": []})

    assert state["locked"] is False
    assert state["last_action"] is None


def test_publish_court_also_publishes_normalized_state():
    from srlobo_mqtt_bridge.discovery import DiscoveryState

    mqtt = MagicMock()
    publisher = TelemetryPublisher(mqtt, MagicMock(), _options(), MagicMock())
    court = CourtDiscovery(index=1, helper_entity_id="light.luces_padel_1", devices=[])
    publisher.on_rediscover(DiscoveryState(courts={1: court}, doors={}))

    mqtt.publish_court_telemetry.assert_called_once()
    mqtt.publish_court_state.assert_called_once()
    assert mqtt.publish_court_state.call_args[0][0] == 1


def test_publish_door_also_publishes_normalized_state():
    from srlobo_mqtt_bridge.discovery import DiscoveryState

    mqtt = MagicMock()
    commands = MagicMock()
    commands.last_door_action.return_value = None
    publisher = TelemetryPublisher(mqtt, MagicMock(), _options(), commands)
    door = DoorDiscovery(index=1, lock_entity_id="lock.puerta_1", devices=[])
    publisher.on_rediscover(DiscoveryState(courts={}, doors={1: door}))

    mqtt.publish_door_telemetry.assert_called_once()
    mqtt.publish_door_state.assert_called_once()
    assert mqtt.publish_door_state.call_args[0][0] == 1


def test_on_state_changed_ignores_entities_not_owned():
    """An event for an entity this bridge doesn't own must never trigger a
    publish. Runtime half of the entity-ownership principle."""
    from srlobo_mqtt_bridge.discovery import DiscoveryState

    ha = MagicMock()
    mqtt = MagicMock()
    publisher = TelemetryPublisher(mqtt, ha, _options(), MagicMock())
    publisher.on_rediscover(DiscoveryState())  # empty, nothing owned
    mqtt.reset_mock()

    publisher.on_state_changed(
        {"entity_id": "binary_sensor.unrelated_solar_helper", "new_state": {"state": "on"}}
    )

    mqtt.publish_court_telemetry.assert_not_called()
    mqtt.publish_door_telemetry.assert_not_called()


def test_build_court_state_payload_extracts_guessed_lux_and_ap():
    publisher = TelemetryPublisher(MagicMock(), MagicMock(), _options(), MagicMock())
    telemetry_payload = {
        "court_index": 1,
        "helper": {"state": "on", "brightness_pct": 50},
        "devices": [
            {
                "member_entity_id": "light.w29",
                "entities": {
                    "light.w29": "on",
                    "sensor.w29_lux": "347.5",
                    "sensor.w29_ssid": "AP-Pista1",
                },
            }
        ],
    }

    state = publisher._build_court_state_payload(telemetry_payload)

    assert state["lux_measured"] == 347.5
    assert state["wifi_ap"] == "AP-Pista1"


def test_build_court_state_payload_ignores_unusable_lux_and_ap_values():
    publisher = TelemetryPublisher(MagicMock(), MagicMock(), _options(), MagicMock())
    telemetry_payload = {
        "court_index": 1,
        "helper": {"state": "on"},
        "devices": [
            {
                "member_entity_id": "light.w29",
                "entities": {"sensor.w29_lux": "unavailable", "sensor.w29_ssid": "unknown"},
            }
        ],
    }

    state = publisher._build_court_state_payload(telemetry_payload)

    assert state["lux_measured"] is None
    assert state["wifi_ap"] is None


def test_build_court_state_payload_reads_mode_from_helper_cache():
    publisher = TelemetryPublisher(MagicMock(), MagicMock(), _options(), MagicMock())
    publisher._cache.seed([{"entity_id": "input_select.modo_pista_1", "state": "LUX_LOOP"}])
    telemetry_payload = {"court_index": 0, "helper": {"state": "on"}, "devices": []}

    state = publisher._build_court_state_payload(telemetry_payload)

    assert state["mode"] == "LUX_LOOP"


def test_build_court_state_payload_drops_mode_outside_contract_enum():
    publisher = TelemetryPublisher(MagicMock(), MagicMock(), _options(), MagicMock())
    publisher._cache.seed([{"entity_id": "input_select.modo_pista_1", "state": "Automatico"}])
    telemetry_payload = {"court_index": 0, "helper": {"state": "on"}, "devices": []}

    state = publisher._build_court_state_payload(telemetry_payload)

    assert state["mode"] is None


def test_state_payloads_carry_firmware_version_from_device_registry():
    commands = MagicMock()
    commands.last_door_action.return_value = None
    publisher = TelemetryPublisher(MagicMock(), MagicMock(), _options(), commands)
    publisher.set_firmware({"light.w1": "1.4.2", "light.w2": "1.4.2", "lock.puerta_1": "4.1"})

    court_state = publisher._build_court_state_payload(
        {"helper": {"state": "on"}, "devices": [{"member_entity_id": "light.w1"}, {"member_entity_id": "light.w2"}]}
    )
    door_state = publisher._build_door_state_payload(0, {"lock": {"entity_id": "lock.puerta_1"}, "devices": []})

    assert court_state["firmware_version"] == "1.4.2"
    assert door_state["firmware_version"] == "4.1"


def test_mixed_firmware_is_listed_not_hidden():
    publisher = TelemetryPublisher(MagicMock(), MagicMock(), _options(), MagicMock())
    publisher.set_firmware({"light.w1": "1.4.2", "light.w2": "1.3.0"})
    state = publisher._build_court_state_payload(
        {"helper": {}, "devices": [{"member_entity_id": "light.w1"}, {"member_entity_id": "light.w2"}]}
    )
    assert state["firmware_version"] == "1.3.0, 1.4.2"


def test_unknown_firmware_is_null():
    publisher = TelemetryPublisher(MagicMock(), MagicMock(), _options(), MagicMock())
    state = publisher._build_court_state_payload({"helper": {}, "devices": [{"member_entity_id": "light.w1"}]})
    assert state["firmware_version"] is None

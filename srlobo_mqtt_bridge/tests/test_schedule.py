import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from srlobo_mqtt_bridge.config import BootstrapConfig, CourtEntity, MqttConfig
from srlobo_mqtt_bridge.entity_registry import EntityRegistry
from srlobo_mqtt_bridge.ha_client import HomeAssistantError
from srlobo_mqtt_bridge.schedule import (
    MODE_CLOUD,
    MODE_ENTITY_ID,
    MODE_OFFLINE_FALLBACK,
    MODE_SCHEDULE_EXPIRED,
    STALE_AFTER_S,
    OfflineScheduler,
    parse_utc,
    validate_schedule,
)

NOW = datetime(2026, 8, 19, 18, 0, tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _payload(**overrides):
    payload = {
        "schema_version": 1,
        "generated_at": _iso(NOW - timedelta(minutes=5)),
        "valid_until": _iso(NOW + timedelta(days=7)),
        "courts": {
            "0": [
                {"from": _iso(NOW - timedelta(minutes=30)), "to": _iso(NOW + timedelta(minutes=30)), "brightness_pct": 80}
            ],
            "1": [],
        },
    }
    payload.update(overrides)
    return payload


def _registry() -> EntityRegistry:
    return EntityRegistry(
        BootstrapConfig(
            installation_id="club_1",
            source_system=None,
            mqtt=MqttConfig(broker="b", base_topic="srlobo/club_1"),
            courts=[CourtEntity(index=0), CourtEntity(index=1)],
        )
    )


class _Clock:
    def __init__(self) -> None:
        self.mono = 1000.0
        self.utc = NOW

    def monotonic(self) -> float:
        return self.mono

    def now(self) -> datetime:
        return self.utc


def _scheduler(tmp_path, ha=None, installation_id="club_1"):
    clock = _Clock()
    ha = ha or MagicMock()
    ha.get_core_info.return_value = {"time_zone": "Europe/Madrid"}
    scheduler = OfflineScheduler(
        ha,
        _registry(),
        installation_id,
        path=str(tmp_path / "schedule.json"),
        monotonic=clock.monotonic,
        now=clock.now,
    )
    return scheduler, ha, clock


# --- validation ---


def test_parse_utc_rejects_timezone_less_and_non_utc_offsets():
    assert parse_utc("2026-08-19T13:30:00Z") == datetime(2026, 8, 19, 13, 30, tzinfo=timezone.utc)
    assert parse_utc("2026-08-19T13:30:00+00:00") is not None
    assert parse_utc("2026-08-19T13:30:00") is None  # no timezone
    assert parse_utc("2026-08-19T15:30:00+02:00") is None
    assert parse_utc(123) is None


def test_valid_snapshot_is_accepted():
    schedule = validate_schedule(_payload(), {0, 1}, NOW)
    assert schedule is not None
    assert schedule.desired_state(0, NOW) == ("on", 80.0)
    assert schedule.desired_state(1, NOW) == ("off", None)


def test_rejects_unsupported_schema_version():
    assert validate_schedule(_payload(schema_version=2), {0, 1}, NOW) is None


def test_rejects_valid_until_in_the_past_unless_allowed():
    payload = _payload(valid_until=_iso(NOW - timedelta(minutes=1)))
    assert validate_schedule(payload, {0, 1}, NOW) is None
    assert validate_schedule(payload, {0, 1}, NOW, allow_expired=True) is not None


def test_rejects_unknown_court_index():
    assert validate_schedule(_payload(courts={"7": []}), {0, 1}, NOW) is None


def test_rejects_to_not_after_from():
    interval = {"from": _iso(NOW), "to": _iso(NOW), "brightness_pct": 50}
    assert validate_schedule(_payload(courts={"0": [interval]}), {0, 1}, NOW) is None


def test_rejects_brightness_out_of_range_or_not_numeric():
    for brightness in (101, -1, "80", True):
        interval = {"from": _iso(NOW), "to": _iso(NOW + timedelta(hours=1)), "brightness_pct": brightness}
        assert validate_schedule(_payload(courts={"0": [interval]}), {0, 1}, NOW) is None, brightness


def test_rejects_overlapping_intervals():
    intervals = [
        {"from": _iso(NOW), "to": _iso(NOW + timedelta(hours=2)), "brightness_pct": 50},
        {"from": _iso(NOW + timedelta(hours=1)), "to": _iso(NOW + timedelta(hours=3)), "brightness_pct": 50},
    ]
    assert validate_schedule(_payload(courts={"0": intervals}), {0, 1}, NOW) is None


def test_adjacent_intervals_are_not_overlapping():
    intervals = [
        {"from": _iso(NOW), "to": _iso(NOW + timedelta(hours=1)), "brightness_pct": 50},
        {"from": _iso(NOW + timedelta(hours=1)), "to": _iso(NOW + timedelta(hours=2)), "brightness_pct": 100},
    ]
    schedule = validate_schedule(_payload(courts={"0": intervals}), {0, 1}, NOW)
    assert schedule is not None
    assert schedule.desired_state(0, NOW + timedelta(hours=1)) == ("on", 100.0)  # end is exclusive


def test_zero_brightness_interval_means_off():
    interval = {"from": _iso(NOW - timedelta(hours=1)), "to": _iso(NOW + timedelta(hours=1)), "brightness_pct": 0}
    schedule = validate_schedule(_payload(courts={"0": [interval]}), {0, 1}, NOW)
    assert schedule.desired_state(0, NOW) == ("off", None)


# --- persistence ---


def test_handle_schedule_persists_atomically_and_reloads(tmp_path):
    scheduler, ha, clock = _scheduler(tmp_path)
    scheduler.handle_schedule(_payload())

    stored = json.loads((tmp_path / "schedule.json").read_text())
    assert stored["installation_id"] == "club_1"
    assert not (tmp_path / "schedule.json.tmp").exists()

    reloaded, _, _ = _scheduler(tmp_path)
    reloaded.load()
    assert reloaded._schedule is not None


def test_invalid_snapshot_keeps_previous_schedule(tmp_path):
    scheduler, ha, clock = _scheduler(tmp_path)
    scheduler.handle_schedule(_payload())
    before = (tmp_path / "schedule.json").read_text()

    scheduler.handle_schedule(_payload(schema_version=99))

    assert (tmp_path / "schedule.json").read_text() == before
    assert scheduler._schedule.desired_state(0, NOW) == ("on", 80.0)


def test_persisted_schedule_from_another_installation_is_ignored(tmp_path):
    master, _, _ = _scheduler(tmp_path, installation_id="master")
    master._registry = _registry()
    master.handle_schedule(_payload())

    clone, _, _ = _scheduler(tmp_path, installation_id="club_1")
    clone.load()
    assert clone._schedule is None


# --- authority and fallback ---


def test_cloud_is_authoritative_while_fresh(tmp_path):
    scheduler, ha, clock = _scheduler(tmp_path)
    scheduler.handle_schedule(_payload())
    clock.mono += STALE_AFTER_S - 1

    assert scheduler.evaluate() == MODE_CLOUD
    ha.call_service.assert_not_called()


def test_startup_grace_period_does_not_take_over_immediately(tmp_path):
    scheduler, ha, clock = _scheduler(tmp_path)
    scheduler.handle_schedule(_payload())
    assert scheduler.evaluate() == MODE_CLOUD
    ha.call_service.assert_not_called()


def test_stale_cloud_switches_to_schedule(tmp_path):
    scheduler, ha, clock = _scheduler(tmp_path)
    scheduler.handle_schedule(_payload())
    clock.mono += STALE_AFTER_S + 1

    assert scheduler.evaluate() == MODE_OFFLINE_FALLBACK
    ha.call_service.assert_any_call("light", "turn_on", "light.luces_padel_1", {"brightness_pct": 80.0})
    ha.call_service.assert_any_call("light", "turn_off", "light.luces_padel_2")


def test_fallback_is_edge_triggered(tmp_path):
    scheduler, ha, clock = _scheduler(tmp_path)
    scheduler.handle_schedule(_payload())
    clock.mono += STALE_AFTER_S + 1
    scheduler.evaluate()
    ha.call_service.reset_mock()

    clock.mono += 15
    scheduler.evaluate()
    ha.call_service.assert_not_called()

    clock.utc = NOW + timedelta(minutes=31)  # court 0's interval has ended
    scheduler.evaluate()
    ha.call_service.assert_called_once_with("light", "turn_off", "light.luces_padel_1")


def test_failed_write_is_retried_next_evaluation(tmp_path):
    ha = MagicMock()
    ha.call_service.side_effect = [HomeAssistantError("down"), None, None, None]
    scheduler, ha, clock = _scheduler(tmp_path, ha=ha)
    scheduler.handle_schedule(_payload())
    clock.mono += STALE_AFTER_S + 1
    scheduler.evaluate()
    scheduler.evaluate()
    on_calls = [c for c in ha.call_service.call_args_list if c.args[1] == "turn_on"]
    assert len(on_calls) == 2


def test_cloud_signal_restores_authority(tmp_path):
    scheduler, ha, clock = _scheduler(tmp_path)
    scheduler.handle_schedule(_payload())
    clock.mono += STALE_AFTER_S + 1
    assert scheduler.evaluate() == MODE_OFFLINE_FALLBACK

    scheduler.mark_cloud_alive()
    ha.call_service.reset_mock()
    assert scheduler.evaluate() == MODE_CLOUD
    ha.call_service.assert_not_called()


def test_expired_schedule_turns_scheduled_courts_off(tmp_path):
    scheduler, ha, clock = _scheduler(tmp_path)
    scheduler.handle_schedule(_payload())
    clock.mono += STALE_AFTER_S + 1
    clock.utc = NOW + timedelta(days=8)

    assert scheduler.evaluate() == MODE_SCHEDULE_EXPIRED
    ha.call_service.assert_any_call("light", "turn_off", "light.luces_padel_1")
    ha.call_service.assert_any_call("light", "turn_off", "light.luces_padel_2")


def test_stale_without_any_schedule_touches_nothing(tmp_path):
    scheduler, ha, clock = _scheduler(tmp_path)
    clock.mono += STALE_AFTER_S + 1
    assert scheduler.evaluate() == MODE_SCHEDULE_EXPIRED
    ha.call_service.assert_not_called()


def test_diagnostic_sensor_reports_mode_and_attributes(tmp_path):
    scheduler, ha, clock = _scheduler(tmp_path)
    scheduler.handle_schedule(_payload())
    scheduler.mark_cloud_alive()
    scheduler.evaluate()

    entity_id, state, attributes = ha.set_state.call_args.args
    assert entity_id == MODE_ENTITY_ID
    assert state == MODE_CLOUD
    assert attributes["schedule_valid_until_utc"] == _iso(NOW + timedelta(days=7))
    assert attributes["last_schedule_sync_utc"] == _iso(NOW)
    assert attributes["last_cloud_message_utc"] == _iso(NOW)
    assert attributes["ha_timezone"] == "Europe/Madrid"


def test_diagnostic_sensor_is_rewritten_periodically(tmp_path):
    scheduler, ha, clock = _scheduler(tmp_path)
    scheduler.evaluate()
    clock.mono += 30
    scheduler.evaluate()
    assert ha.set_state.call_count == 1
    clock.mono += 31
    scheduler.evaluate()
    assert ha.set_state.call_count == 2

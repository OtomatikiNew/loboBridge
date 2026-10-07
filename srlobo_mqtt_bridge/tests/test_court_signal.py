import json
from unittest.mock import MagicMock

import pytest

from srlobo_mqtt_bridge.config import BootstrapConfig, CourtEntity, MqttConfig
from srlobo_mqtt_bridge.court_signal import (
    SOURCE_CLOUD,
    SOURCE_OFFLINE_SCHEDULE,
    CourtSignalPublisher,
    normalize,
)
from srlobo_mqtt_bridge.entity_registry import EntityRegistry
from srlobo_mqtt_bridge.ha_client import HomeAssistantError


def _registry() -> EntityRegistry:
    return EntityRegistry(
        BootstrapConfig(
            installation_id="club_1",
            source_system=None,
            mqtt=MqttConfig(broker="b", base_topic="srlobo/club_1"),
            courts=[CourtEntity(index=0), CourtEntity(index=1)],
        )
    )


def _publisher(tmp_path, ha=None, installation_id="club_1"):
    ha = ha or MagicMock()
    publisher = CourtSignalPublisher(
        ha, _registry(), installation_id, path=str(tmp_path / "signals.json"), now_iso=lambda: "2026-10-07T12:00:00Z"
    )
    return publisher, ha


@pytest.mark.parametrize(
    "state, brightness, expected",
    [
        ("off", None, ("off", 0)),
        ("off", 80, ("off", 0)),
        ("on", None, ("on", 100)),
        ("on", 70, ("on", 70)),
        (None, 45.6, ("on", 46)),
        ("on", 150, ("on", 100)),
        ("on", -5, ("off", 0)),
        ("on", 0, ("off", 0)),
        (None, None, None),
        ("on", "bad", ("on", 100)),
    ],
)
def test_normalize_only_ever_produces_on_or_off(state, brightness, expected):
    assert normalize(state, brightness) == expected


def test_apply_writes_binary_sensor_with_1_0_contract(tmp_path):
    publisher, ha = _publisher(tmp_path)
    assert publisher.apply(0, "on", 70, SOURCE_CLOUD) is True
    entity_id, state, attributes = ha.set_state.call_args.args
    assert entity_id == "binary_sensor.pista_1"
    assert state == "on"
    assert attributes["brightness"] == 70
    assert attributes["friendly_name"] == "Pista 1"
    ha.call_service.assert_not_called()


def test_mode_only_payload_is_not_applied(tmp_path):
    publisher, ha = _publisher(tmp_path)
    assert publisher.apply(0, None, None, SOURCE_CLOUD) is False
    ha.set_state.assert_not_called()


def test_identical_rewrite_keeps_same_attributes(tmp_path):
    """A rewrite with identical state and attributes doesn't fire
    state_changed in HA, so periodic refreshes don't retrigger automations."""
    times = iter(["2026-10-07T12:00:00Z", "2026-10-07T12:05:00Z"])
    ha = MagicMock()
    publisher = CourtSignalPublisher(
        ha, _registry(), "club_1", path=str(tmp_path / "s.json"), now_iso=lambda: next(times)
    )
    publisher.apply(0, "on", 70, SOURCE_CLOUD)
    first = ha.set_state.call_args.args
    publisher.apply(0, "on", 70, SOURCE_CLOUD)
    publisher.republish()
    assert ha.set_state.call_args.args == first


def test_signal_survives_restart_and_is_rewritten_on_load(tmp_path):
    publisher, _ = _publisher(tmp_path)
    publisher.apply(0, "on", 60, SOURCE_CLOUD)
    publisher.apply(1, "off", None, SOURCE_OFFLINE_SCHEDULE)

    restarted, ha = _publisher(tmp_path)
    restarted.load()
    written = {c.args[0]: (c.args[1], c.args[2]["brightness"]) for c in ha.set_state.call_args_list}
    assert written == {"binary_sensor.pista_1": ("on", 60), "binary_sensor.pista_2": ("off", 0)}


def test_republish_after_ha_restart_rewrites_every_court(tmp_path):
    publisher, ha = _publisher(tmp_path)
    publisher.apply(0, "on", 60, SOURCE_CLOUD)
    publisher.apply(1, "on", 30, SOURCE_CLOUD)
    ha.set_state.reset_mock()
    publisher.republish()
    assert {c.args[0] for c in ha.set_state.call_args_list} == {"binary_sensor.pista_1", "binary_sensor.pista_2"}


def test_signals_from_another_installation_are_ignored(tmp_path):
    (tmp_path / "signals.json").write_text(
        json.dumps({"installation_id": "other_club", "courts": {"0": {"state": "on", "brightness": 90}}})
    )
    publisher, ha = _publisher(tmp_path)
    publisher.load()
    ha.set_state.assert_not_called()
    assert publisher.current(0) is None


def test_invalid_saved_values_are_ignored(tmp_path):
    (tmp_path / "signals.json").write_text(
        json.dumps(
            {
                "installation_id": "club_1",
                "courts": {"0": {"state": "dim", "brightness": 13}, "7": {"state": "on"}, "1": {"state": "on", "brightness": 40}},
            }
        )
    )
    publisher, ha = _publisher(tmp_path)
    publisher.load()
    assert [c.args[0] for c in ha.set_state.call_args_list] == ["binary_sensor.pista_2"]


def test_failed_write_is_kept_and_written_on_next_refresh(tmp_path):
    ha = MagicMock()
    ha.set_state.side_effect = [HomeAssistantError("core restarting"), None]
    publisher, ha = _publisher(tmp_path, ha=ha)
    with pytest.raises(HomeAssistantError):
        publisher.apply(0, "on", 50, SOURCE_CLOUD)
    assert publisher.current(0) == ("on", 50)
    publisher.republish()
    assert ha.set_state.call_args.args[1] == "on"


def test_court_names_from_venue_config_are_used(tmp_path):
    publisher, ha = _publisher(tmp_path)
    publisher.apply(0, "on", 70, SOURCE_CLOUD)
    publisher.set_court_names({0: "Premier Official Court -blau-"})
    assert ha.set_state.call_args.args[2]["friendly_name"] == "Premier Official Court -blau-"


def test_custom_entity_template(tmp_path):
    ha = MagicMock()
    publisher = CourtSignalPublisher(
        ha, _registry(), "club_1", entity_template="binary_sensor.srlobo_pista_{n}", path=str(tmp_path / "s.json")
    )
    publisher.apply(1, "off", None, SOURCE_CLOUD)
    assert ha.set_state.call_args.args[0] == "binary_sensor.srlobo_pista_2"

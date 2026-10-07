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
    ticks = iter(f"2026-10-07T12:{m:02d}:00Z" for m in range(60))
    ha = MagicMock()
    ha.get_state.return_value = None
    publisher = CourtSignalPublisher(
        ha, _registry(), "club_1", path=str(tmp_path / "s.json"), now_iso=lambda: next(ticks)
    )
    publisher.apply(0, "on", 70, SOURCE_CLOUD)
    publisher.apply(0, "on", 70, SOURCE_CLOUD)
    publisher.republish()
    publisher.republish()
    court_1 = [c.args for c in ha.set_state.call_args_list if c.args[0] == "binary_sensor.pista_1"]
    assert len(court_1) == 4
    assert all(args == court_1[0] for args in court_1)


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
    ha = MagicMock()
    ha.get_state.return_value = None  # no light group
    publisher, ha = _publisher(tmp_path, ha=ha)
    publisher.load()
    # The other club's "on 90" is never used; the court starts from its own lights.
    assert publisher.current(0) == ("off", 0)


def test_invalid_saved_values_are_ignored(tmp_path):
    (tmp_path / "signals.json").write_text(
        json.dumps(
            {
                "installation_id": "club_1",
                "courts": {"0": {"state": "dim", "brightness": 13}, "7": {"state": "on"}, "1": {"state": "on", "brightness": 40}},
            }
        )
    )
    ha = MagicMock()
    ha.get_state.return_value = None
    publisher, ha = _publisher(tmp_path, ha=ha)
    publisher.load()
    assert publisher.current(1) == ("on", 40)  # valid saved value kept
    assert publisher.current(0) == ("off", 0)  # "dim" discarded, initialised instead


def test_failed_write_is_kept_and_written_on_next_refresh(tmp_path):
    ha = MagicMock()
    ha.get_state.return_value = None
    failures = {"left": 1}

    def set_state(entity_id, state, attributes):
        if failures["left"]:
            failures["left"] -= 1
            raise HomeAssistantError("core restarting")

    ha.set_state.side_effect = set_state
    publisher, ha = _publisher(tmp_path, ha=ha)
    with pytest.raises(HomeAssistantError):
        publisher.apply(0, "on", 50, SOURCE_CLOUD)
    assert publisher.current(0) == ("on", 50)
    publisher.republish()
    written = {c.args[0]: c.args[1] for c in ha.set_state.call_args_list}
    assert written["binary_sensor.pista_1"] == "on"


def test_court_names_from_venue_config_are_used(tmp_path):
    publisher, ha = _publisher(tmp_path)
    publisher.apply(0, "on", 70, SOURCE_CLOUD)
    publisher.set_court_names({0: "Premier Official Court -blau-"})
    names = {c.args[0]: c.args[2]["friendly_name"] for c in ha.set_state.call_args_list}
    assert names["binary_sensor.pista_1"] == "Premier Official Court -blau-"
    assert names["binary_sensor.pista_2"] == "Pista 2"


def test_custom_entity_template(tmp_path):
    ha = MagicMock()
    publisher = CourtSignalPublisher(
        ha, _registry(), "club_1", entity_template="binary_sensor.srlobo_pista_{n}", path=str(tmp_path / "s.json")
    )
    publisher.apply(1, "off", None, SOURCE_CLOUD)
    assert ha.set_state.call_args.args[0] == "binary_sensor.srlobo_pista_2"


# --- every court gets its sensor from startup ---


def _lights(states):
    def get_state(entity_id):
        return states.get(entity_id)

    return get_state


def test_every_court_gets_a_sensor_at_startup_copied_from_its_lights(tmp_path):
    ha = MagicMock()
    ha.get_state.side_effect = _lights(
        {"light.luces_padel_2": {"state": "on", "attributes": {"brightness": 128}}}
    )
    publisher, ha = _publisher(tmp_path, ha=ha)
    publisher.load()  # nothing saved yet

    written = {c.args[0]: (c.args[1], c.args[2]["brightness"], c.args[2]["source"]) for c in ha.set_state.call_args_list}
    assert written == {
        "binary_sensor.pista_1": ("off", 0, "initial"),  # no lights in HA
        "binary_sensor.pista_2": ("on", 50, "initial"),  # 128/255 -> 50 %
    }
    ha.call_service.assert_not_called()


def test_initial_signal_is_saved_and_not_recomputed(tmp_path):
    ha = MagicMock()
    ha.get_state.return_value = {"state": "on", "attributes": {"brightness": 255}}
    publisher, _ = _publisher(tmp_path, ha=ha)
    publisher.load()

    restarted_ha = MagicMock()
    restarted_ha.get_state.return_value = {"state": "off"}
    restarted, restarted_ha = _publisher(tmp_path, ha=restarted_ha)
    restarted.load()
    assert restarted.current(0) == ("on", 100)  # saved value wins over the lights now
    restarted_ha.get_state.assert_not_called()


def test_court_is_not_guessed_off_when_ha_cannot_be_read(tmp_path):
    ha = MagicMock()
    ha.get_state.side_effect = ConnectionError("core starting")
    publisher, ha = _publisher(tmp_path, ha=ha)
    publisher.load()
    ha.set_state.assert_not_called()
    assert publisher.current(0) is None

    ha.get_state.side_effect = None
    ha.get_state.return_value = {"state": "on", "attributes": {"brightness": 51}}
    publisher.republish()  # next refresh
    assert publisher.current(0) == ("on", 20)


def test_cloud_signal_is_not_overwritten_by_initialisation(tmp_path):
    ha = MagicMock()
    ha.get_state.return_value = {"state": "off"}
    publisher, ha = _publisher(tmp_path, ha=ha)
    publisher.apply(0, "on", 70, SOURCE_CLOUD)
    publisher.republish()
    assert publisher.current(0) == ("on", 70)
    assert publisher.current(1) == ("off", 0)

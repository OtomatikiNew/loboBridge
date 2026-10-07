from unittest.mock import MagicMock

from srlobo_mqtt_bridge.config import AddonOptions
from srlobo_mqtt_bridge.dashboard import (
    DASHBOARD_URL_PATH,
    DashboardProvisioner,
    build_dashboard_config,
    missing_court_entities,
)
from srlobo_mqtt_bridge.discovery import CourtDiscovery, DiscoveryState, DoorDiscovery
from srlobo_mqtt_bridge.schedule import MODE_ENTITY_ID
from srlobo_mqtt_bridge.venue_config import VenueConfig


def _options() -> AddonOptions:
    return AddonOptions(srlobo_token="t", srlobo_api_url="https://x", bootstrap_path="/x", log_level="info")


def _venue() -> VenueConfig:
    return VenueConfig(
        schema_version=1,
        club_name="Club Uno",
        address=None,
        latitude=0.0,
        longitude=0.0,
        timezone="Europe/Madrid",
        language="es",
        booking_system=None,
        court_names={0: "Pista Central"},
    )


def _discovery(states) -> DiscoveryState:
    return DiscoveryState(
        courts={
            0: CourtDiscovery(index=0, helper_entity_id="light.luces_padel_1"),
            1: CourtDiscovery(index=1, helper_entity_id="light.luces_padel_2"),
        },
        doors={0: DoorDiscovery(index=0, lock_entity_id="lock.puerta_1")},
        raw_states=[{"entity_id": entity_id, "state": "on"} for entity_id in states],
    )


def test_build_config_uses_court_names_and_only_existing_entities():
    config = build_dashboard_config(
        court_indexes=[0, 1],
        door_entities={0: "lock.puerta_1"},
        existing_entities={"light.luces_padel_1", "input_boolean.regulacion_por_lux_pista_1", "lock.puerta_1"},
        options=_options(),
        venue=_venue(),
    )

    assert config["title"] == "Club Uno"
    cards = config["views"][0]["cards"]
    court_card = cards[0]["cards"]
    assert court_card[0]["entity"] == "light.luces_padel_1"
    assert court_card[0]["name"] == "Pista Central"
    assert court_card[1]["entities"] == ["input_boolean.regulacion_por_lux_pista_1"]  # lux reference missing, left out
    assert all("light.luces_padel_2" not in str(card) for card in cards)  # court 2 helper missing
    assert {"type": "tile", "entity": "lock.puerta_1"} in cards
    assert cards[-1]["entities"] == [MODE_ENTITY_ID]


def test_build_config_falls_back_to_numbered_names():
    config = build_dashboard_config([1], {}, {"light.luces_padel_2"}, _options())
    assert config["views"][0]["cards"][0]["cards"][0]["name"] == "Pista 2"


def test_light_tile_has_no_tap_interaction():
    """ADR-026: the light tile is status-only. A tappable brightness slider
    on the dashboard would let someone bypass the local automation exactly
    like the bridge itself no longer does."""
    config = build_dashboard_config([0], {}, {"light.luces_padel_1"}, _options())
    light_tile = config["views"][0]["cards"][0]["cards"][0]
    assert "features" not in light_tile
    assert light_tile["tap_action"] == {"action": "none"}


def test_auto_manual_tile_shown_when_entity_exists():
    config = build_dashboard_config(
        [0], {}, {"light.luces_padel_1", "input_boolean.auto_manual_luz_1"}, _options()
    )
    court_card = config["views"][0]["cards"][0]["cards"]
    assert {"type": "tile", "entity": "input_boolean.auto_manual_luz_1", "name": "Pista 1 - Auto/Manual"} in court_card


def test_auto_manual_tile_omitted_when_entity_missing():
    config = build_dashboard_config([0], {}, {"light.luces_padel_1"}, _options())
    court_card = config["views"][0]["cards"][0]["cards"]
    assert all("auto_manual_luz" not in str(card) for card in court_card)


def test_missing_court_entities_lists_blueprint_gaps():
    missing = missing_court_entities([0], {"light.luces_padel_1", "input_boolean.regulacion_por_lux_pista_1"}, _options())
    assert missing == ["input_number.referencia_de_lux_pista_1", "input_button.fijar_referencia_pista_1"]


def test_provisioner_creates_dashboard_once_and_saves_config():
    ha = MagicMock()
    ha.ws_command.side_effect = lambda message: [] if message["type"] == "lovelace/dashboards/list" else None
    provisioner = DashboardProvisioner(ha, _options())

    provisioner.update_discovery(_discovery(["light.luces_padel_1"]))

    types = [call.args[0]["type"] for call in ha.ws_command.call_args_list]
    assert types == ["lovelace/dashboards/list", "lovelace/dashboards/create", "lovelace/config/save"]
    create = ha.ws_command.call_args_list[1].args[0]
    assert create["url_path"] == DASHBOARD_URL_PATH
    assert ha.ws_command.call_args_list[2].args[0]["url_path"] == DASHBOARD_URL_PATH


def test_provisioner_only_touches_its_own_dashboard():
    ha = MagicMock()
    ha.ws_command.side_effect = (
        lambda message: [{"url_path": DASHBOARD_URL_PATH}] if message["type"] == "lovelace/dashboards/list" else None
    )
    provisioner = DashboardProvisioner(ha, _options())
    provisioner.update_discovery(_discovery(["light.luces_padel_1"]))

    for call in ha.ws_command.call_args_list:
        message = call.args[0]
        assert message["type"] != "lovelace/dashboards/create"
        if "url_path" in message:
            assert message["url_path"] == DASHBOARD_URL_PATH


def test_provisioner_skips_unchanged_config_and_follows_renames():
    ha = MagicMock()
    ha.ws_command.return_value = [{"url_path": DASHBOARD_URL_PATH}]
    provisioner = DashboardProvisioner(ha, _options())
    discovery = _discovery(["light.luces_padel_1"])

    provisioner.update_discovery(discovery)
    saves = lambda: [c for c in ha.ws_command.call_args_list if c.args[0]["type"] == "lovelace/config/save"]
    assert len(saves()) == 1

    provisioner.update_discovery(discovery)
    assert len(saves()) == 1  # nothing changed

    provisioner.update_venue(_venue())
    assert len(saves()) == 2
    assert saves()[-1].args[0]["config"]["title"] == "Club Uno"


def test_provisioner_failure_never_raises():
    ha = MagicMock()
    ha.ws_command.side_effect = RuntimeError("HA down")
    provisioner = DashboardProvisioner(ha, _options())
    provisioner.update_discovery(_discovery(["light.luces_padel_1"]))  # must not raise

from unittest.mock import MagicMock

from srlobo_mqtt_bridge.config import BootstrapConfig, CourtEntity, MqttConfig
from srlobo_mqtt_bridge.device_registry import RegistrySnapshot
from srlobo_mqtt_bridge.discovery import Discoverer
from srlobo_mqtt_bridge.entity_registry import EntityRegistry


def _bootstrap(court_count: int) -> BootstrapConfig:
    """A bootstrap config with `court_count` courts and no doors."""
    return BootstrapConfig(
        installation_id="inst-1",
        source_system=None,
        mqtt=MagicMock(spec=MqttConfig),
        courts=[CourtEntity(index=i) for i in range(court_count)],
    )


def _discover(states, registry_entities, court_count, members_by_court=None):
    """Runs discovery over fake HA states.

    Args:
        states: Entity ids present in HA.
        registry_entities: Entity registry entries (entity_id, device_id).
        court_count: How many courts the club has.
        members_by_court: Light group members per court number; when left
            out, the court's light group has no members.

    Returns:
        The discovery state.
    """
    members_by_court = members_by_court or {}
    by_id = {entity_id: {"entity_id": entity_id, "state": "on", "attributes": {}} for entity_id in states}
    for number, members in members_by_court.items():
        by_id[f"light.luces_padel_{number}"]["attributes"] = {"entity_id": members}
    ha = MagicMock()
    ha.get_states.return_value = list(by_id.values())
    ha.get_state.side_effect = by_id.get
    bootstrap = _bootstrap(court_count)
    discoverer = Discoverer(ha, EntityRegistry(bootstrap), bootstrap)
    return discoverer.discover_all(RegistrySnapshot(entities=registry_entities))


def _court_entities(discovery, index):
    return [entity_id for device in discovery.courts[index].devices for entity_id in device.entity_ids]


def test_luces_padel_1_does_not_pick_up_luces_padel_10_to_19():
    states = [f"light.luces_padel_{n}" for n in range(1, 20)]
    states += [f"sensor.luces_padel_{n}_lux" for n in range(1, 20)]

    discovery = _discover(states, [], court_count=19)

    assert sorted(_court_entities(discovery, 0)) == ["light.luces_padel_1", "sensor.luces_padel_1_lux"]
    assert sorted(_court_entities(discovery, 9)) == ["light.luces_padel_10", "sensor.luces_padel_10_lux"]


def test_wr_1_fp_does_not_pick_up_wr_10_fp():
    states = ["light.luces_padel_1", "light.luces_padel_10"]
    registry = []
    for n in (1, 10):
        states += [f"light.wr_{n}_fp", f"sensor.wr_{n}_fp_potencia", f"sensor.wr_{n}_fp_wifi_rssi"]
        registry += [
            {"entity_id": f"light.wr_{n}_fp", "device_id": f"dev_{n}"},
            {"entity_id": f"sensor.wr_{n}_fp_potencia", "device_id": f"dev_{n}"},
            {"entity_id": f"sensor.wr_{n}_fp_wifi_rssi", "device_id": f"dev_{n}"},
        ]

    discovery = _discover(
        states, registry, court_count=10,
        members_by_court={1: ["light.wr_1_fp"], 10: ["light.wr_10_fp"]},
    )

    assert sorted(_court_entities(discovery, 0)) == [
        "light.wr_1_fp", "sensor.wr_1_fp_potencia", "sensor.wr_1_fp_wifi_rssi",
    ]
    assert "sensor.wr_1_fp_potencia" not in _court_entities(discovery, 9)


def test_siblings_come_from_the_device_not_the_name():
    states = ["light.luces_padel_1", "light.wr_1_fp", "sensor.shelly_pro_power", "sensor.wr_1_fp_other_device"]
    registry = [
        {"entity_id": "light.wr_1_fp", "device_id": "dev_1"},
        {"entity_id": "sensor.shelly_pro_power", "device_id": "dev_1"},
        {"entity_id": "sensor.wr_1_fp_other_device", "device_id": "dev_2"},
    ]

    discovery = _discover(states, registry, court_count=1, members_by_court={1: ["light.wr_1_fp"]})

    assert sorted(_court_entities(discovery, 0)) == ["light.wr_1_fp", "sensor.shelly_pro_power"]


def test_template_sensor_without_a_device_is_still_found():
    states = [
        "light.luces_padel_1", "light.luces_padel_10",
        "light.wr_1_fp", "light.wr_10_fp",
        "sensor.wr_1_fp_potencia_linea", "sensor.wr_10_fp_potencia_linea",
    ]
    registry = [
        {"entity_id": "light.wr_1_fp", "device_id": "dev_1"},
        {"entity_id": "light.wr_10_fp", "device_id": "dev_10"},
        {"entity_id": "sensor.wr_1_fp_potencia_linea", "device_id": None},
        {"entity_id": "sensor.wr_10_fp_potencia_linea", "device_id": None},
    ]

    discovery = _discover(
        states, registry, court_count=10,
        members_by_court={1: ["light.wr_1_fp"], 10: ["light.wr_10_fp"]},
    )

    assert sorted(_court_entities(discovery, 0)) == ["light.wr_1_fp", "sensor.wr_1_fp_potencia_linea"]
    assert sorted(_court_entities(discovery, 9)) == ["light.wr_10_fp", "sensor.wr_10_fp_potencia_linea"]


def test_without_a_registry_snapshot_names_still_need_the_underscore():
    states = ["light.luces_padel_1", "light.wr_1_fp", "sensor.wr_1_fp_potencia", "sensor.wr_10_fp_potencia"]
    ha = MagicMock()
    by_id = {entity_id: {"entity_id": entity_id, "attributes": {}} for entity_id in states}
    by_id["light.luces_padel_1"]["attributes"] = {"entity_id": ["light.wr_1_fp"]}
    ha.get_states.return_value = list(by_id.values())
    ha.get_state.side_effect = by_id.get
    bootstrap = _bootstrap(1)

    discovery = Discoverer(ha, EntityRegistry(bootstrap), bootstrap).discover_all()

    assert sorted(_court_entities(discovery, 0)) == ["light.wr_1_fp", "sensor.wr_1_fp_potencia"]

from srlobo_mqtt_bridge.config import BootstrapConfig, CourtEntity, DoorEntity, MqttConfig
from srlobo_mqtt_bridge.entity_registry import EntityRegistry, court_helper_entity_id


def _bootstrap() -> BootstrapConfig:
    return BootstrapConfig(
        installation_id="club_1",
        source_system="playtomic",
        mqtt=MqttConfig(broker="b", base_topic="srlobo/club_1"),
        courts=[CourtEntity(index=1), CourtEntity(index=2)],
        doors=[DoorEntity(index=1, entity_id="lock.puerta_1")],
    )


def test_owns_court_helper_entities():
    registry = EntityRegistry(_bootstrap())
    assert registry.is_owned(court_helper_entity_id(1))
    assert registry.is_owned(court_helper_entity_id(2))
    assert not registry.is_owned(court_helper_entity_id(3))


def test_owns_door_entity_from_bootstrap():
    registry = EntityRegistry(_bootstrap())
    assert registry.is_owned("lock.puerta_1")
    assert registry.door_entity_id(1) == "lock.puerta_1"


def test_does_not_own_unrelated_entities():
    """This is the structural guard against the loboBrain domain-filter bug:
    an entity is never 'owned' just because it shares a domain with something
    we do own."""
    registry = EntityRegistry(_bootstrap())
    assert not registry.is_owned("binary_sensor.some_unrelated_solar_helper")
    assert not registry.is_owned("light.luces_padel_99")


def test_register_member_entity_extends_ownership():
    registry = EntityRegistry(_bootstrap())
    assert not registry.is_owned("sensor.w29_potencia")
    registry.register_member_entity("sensor.w29_potencia")
    assert registry.is_owned("sensor.w29_potencia")


def test_door_entity_id_raises_for_unconfigured_door():
    registry = EntityRegistry(_bootstrap())
    try:
        registry.door_entity_id(99)
        assert False, "expected KeyError"
    except KeyError:
        pass


def test_court_helper_uses_one_based_number_for_zero_based_wire_index():
    assert court_helper_entity_id(0) == "light.luces_padel_1"
    assert court_helper_entity_id(1) == "light.luces_padel_2"

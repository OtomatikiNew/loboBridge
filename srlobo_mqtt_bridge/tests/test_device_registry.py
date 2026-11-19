from unittest.mock import MagicMock

from srlobo_mqtt_bridge.device_registry import DisabledEntityEnabler, RegistrySnapshot
from srlobo_mqtt_bridge.ha_client import HomeAssistantError


def _snapshot() -> RegistrySnapshot:
    return RegistrySnapshot(
        devices={
            "dev_w1": {"id": "dev_w1", "manufacturer": "Shelly", "sw_version": "1.4.2"},
            "dev_w2": {"id": "dev_w2", "manufacturer": "Shelly", "sw_version": "1.4.2"},
            "dev_nuki": {"id": "dev_nuki", "manufacturer": "Nuki Home Solutions GmbH", "sw_version": "4.1"},
            "dev_other": {"id": "dev_other", "manufacturer": "Acme", "sw_version": "9"},
        },
        entities=[
            {"entity_id": "light.w1", "device_id": "dev_w1", "platform": "shelly", "disabled_by": None, "config_entry_id": "e1"},
            {"entity_id": "sensor.w1_potencia", "device_id": "dev_w1", "platform": "shelly", "disabled_by": "integration", "config_entry_id": "e1"},
            {"entity_id": "sensor.w1_energia", "device_id": "dev_w1", "platform": "shelly", "disabled_by": "user", "config_entry_id": "e1"},
            {"entity_id": "sensor.w2_potencia", "device_id": "dev_w2", "platform": "shelly", "disabled_by": "integration", "config_entry_id": "e2"},
            {"entity_id": "sensor.nuki_battery", "device_id": "dev_nuki", "platform": "nuki", "disabled_by": "integration", "config_entry_id": "e3"},
            {"entity_id": "sensor.acme_x", "device_id": "dev_other", "platform": "acme", "disabled_by": "integration", "config_entry_id": "e4"},
        ],
    )


def _enabled_entities(ha) -> list:
    return [call.args[0] for call in ha.enable_entity.call_args_list]


def test_reenables_integration_disabled_entities_on_seed_and_manufacturer_devices():
    ha = MagicMock()
    enabled = DisabledEntityEnabler(ha, ["Shelly", "Nuki"]).run(_snapshot(), ["light.w1"])

    assert enabled == 3
    assert sorted(_enabled_entities(ha)) == ["sensor.nuki_battery", "sensor.w1_potencia", "sensor.w2_potencia"]
    assert sorted(call.args[0] for call in ha.reload_config_entry.call_args_list) == ["e1", "e2", "e3"]


def test_never_reenables_an_entity_a_person_disabled():
    ha = MagicMock()
    DisabledEntityEnabler(ha, ["Shelly"]).run(_snapshot(), ["light.w1"])
    assert "sensor.w1_energia" not in _enabled_entities(ha)


def test_unknown_manufacturer_only_covered_through_seeds():
    ha = MagicMock()
    DisabledEntityEnabler(ha, ["Shelly"]).run(_snapshot(), [])
    assert "sensor.acme_x" not in _enabled_entities(ha)
    assert "sensor.nuki_battery" not in _enabled_entities(ha)


def test_scoped_to_a_newly_added_device():
    ha = MagicMock()
    DisabledEntityEnabler(ha, ["Shelly", "Nuki"]).run(_snapshot(), ["light.w1"], only_device_ids={"dev_w2"})
    assert _enabled_entities(ha) == ["sensor.w2_potencia"]


def test_one_failed_enable_does_not_stop_the_rest():
    ha = MagicMock()
    ha.enable_entity.side_effect = [HomeAssistantError("nope"), None, None]
    enabled = DisabledEntityEnabler(ha, ["Shelly", "Nuki"]).run(_snapshot(), [])
    assert enabled == 2


def test_never_reenables_companion_app_entities_on_a_shelly_wall_display():
    snapshot = RegistrySnapshot(
        devices={"dev_blake": {"id": "dev_blake", "manufacturer": "Shelly", "name": "Blake"}},
        entities=[
            {"entity_id": "sensor.blake_next_alarm", "device_id": "dev_blake", "platform": "mobile_app",
             "disabled_by": "integration", "config_entry_id": "app"},
            {"entity_id": "sensor.blake_ip", "device_id": "dev_blake", "platform": "mobile_app",
             "disabled_by": "integration", "config_entry_id": "app"},
        ],
    )
    ha = MagicMock()

    enabled = DisabledEntityEnabler(ha, ["Shelly"]).run(snapshot, [])

    assert enabled == 0
    ha.enable_entity.assert_not_called()
    ha.reload_config_entry.assert_not_called()


def test_never_reenables_firmware_update_or_button_entities():
    snapshot = RegistrySnapshot(
        devices={"dev_w1": {"id": "dev_w1", "manufacturer": "Shelly"}},
        entities=[
            {"entity_id": "update.w1_firmware_beta", "device_id": "dev_w1", "platform": "shelly",
             "disabled_by": "integration", "config_entry_id": "e1"},
            {"entity_id": "button.w1_reboot", "device_id": "dev_w1", "platform": "shelly",
             "disabled_by": "integration", "config_entry_id": "e1"},
            {"entity_id": "sensor.w1_potencia", "device_id": "dev_w1", "platform": "shelly",
             "disabled_by": "integration", "config_entry_id": "e1"},
        ],
    )
    ha = MagicMock()

    DisabledEntityEnabler(ha, ["Shelly"]).run(snapshot, ["light.w1"])

    assert _enabled_entities(ha) == ["sensor.w1_potencia"]


def test_seed_device_from_another_integration_is_not_reenabled():
    ha = MagicMock()
    DisabledEntityEnabler(ha, []).run(_snapshot(), ["sensor.acme_x"])
    assert _enabled_entities(ha) == []


def test_device_id_by_entity_leaves_out_entities_without_a_device():
    snapshot = RegistrySnapshot(entities=[
        {"entity_id": "light.wr_1_fp", "device_id": "dev_1"},
        {"entity_id": "sensor.wr_1_fp_potencia_linea", "device_id": None},
    ])
    assert snapshot.device_id_by_entity() == {"light.wr_1_fp": "dev_1"}


def test_firmware_by_entity_uses_device_sw_version():
    firmware = _snapshot().firmware_by_entity()
    assert firmware["light.w1"] == "1.4.2"
    assert firmware["sensor.nuki_battery"] == "4.1"

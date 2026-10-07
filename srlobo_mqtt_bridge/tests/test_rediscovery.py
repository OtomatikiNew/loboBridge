from unittest.mock import MagicMock, patch

from srlobo_mqtt_bridge.discovery import DiscoveryState
from srlobo_mqtt_bridge.rediscovery import RediscoveryCoordinator


def _coordinator(enabled=0, registry_error=None):
    ha = MagicMock()
    if registry_error:
        ha.list_device_registry.side_effect = registry_error
    else:
        ha.list_device_registry.return_value = [{"id": "d1", "sw_version": "1.0"}]
        ha.list_entity_registry.return_value = [{"entity_id": "light.w1", "device_id": "d1"}]
    discoverer = MagicMock()
    discoverer.seed_entity_ids.return_value = ["light.w1"]
    discoverer.discover_all.return_value = DiscoveryState()
    enabler = MagicMock()
    enabler.run.return_value = enabled
    telemetry, dashboard, health = MagicMock(), MagicMock(), MagicMock()
    coordinator = RediscoveryCoordinator(ha, discoverer, enabler, telemetry, dashboard, health)
    return coordinator, discoverer, enabler, telemetry, health


def test_reconnect_runs_reenable_pass_before_discovery_and_sets_firmware():
    coordinator, discoverer, enabler, telemetry, health = _coordinator()
    order = []
    enabler.run.side_effect = lambda *a, **k: order.append("enable") or 0
    discoverer.discover_all.side_effect = lambda: order.append("discover") or DiscoveryState()

    coordinator.on_reconnect()

    assert order == ["enable", "discover"]
    health.record_reconnect.assert_called_once()
    telemetry.set_firmware.assert_called_once_with({"light.w1": "1.0"})
    telemetry.on_rediscover.assert_called_once()


def test_registry_failure_still_runs_discovery():
    coordinator, discoverer, enabler, telemetry, health = _coordinator(registry_error=RuntimeError("ws down"))
    coordinator.on_reconnect()
    enabler.run.assert_not_called()
    discoverer.discover_all.assert_called_once()


def test_follow_up_discovery_scheduled_only_when_something_was_enabled():
    with patch("srlobo_mqtt_bridge.rediscovery.threading.Timer") as timer:
        _coordinator(enabled=0)[0].on_reconnect()
        timer.assert_not_called()
        _coordinator(enabled=2)[0].on_reconnect()
        timer.assert_called_once()


def test_device_events_are_debounced_and_scoped():
    coordinator, discoverer, enabler, telemetry, health = _coordinator()
    with patch("srlobo_mqtt_bridge.rediscovery.threading.Timer") as timer:
        coordinator.on_device_registry_updated({"action": "create", "device_id": "d1"})
        coordinator.on_device_registry_updated({"action": "update", "device_id": "d2"})
        coordinator.on_device_registry_updated({"action": "remove", "device_id": "d3"})
        assert timer.call_count == 2  # remove is ignored

    coordinator._flush_device_events()
    assert enabler.run.call_args.args[2] == {"d1", "d2"}
    health.record_reconnect.assert_not_called()

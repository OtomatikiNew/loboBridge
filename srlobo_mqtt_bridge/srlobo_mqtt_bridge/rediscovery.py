"""Re-runs entity discovery when HA changes.

Triggers: every HA WebSocket (re)connect, device_registry_updated (a Shelly
added after install, debounced since one device fires several events), and
a follow-up 30s after entities were re-enabled, once the integration has
created their states. Runs never overlap.
"""

import logging
import threading
from typing import Any, Dict, Optional, Set

from .dashboard import DashboardProvisioner
from .device_registry import DisabledEntityEnabler, RegistrySnapshot
from .discovery import Discoverer
from .ha_client import HomeAssistantClient
from .health import HealthReporter
from .telemetry import TelemetryPublisher

logger = logging.getLogger(__name__)

DEVICE_EVENT_DEBOUNCE_S = 10.0
FOLLOW_UP_DELAY_S = 30.0


class RediscoveryCoordinator:
    def __init__(
        self,
        ha: HomeAssistantClient,
        discoverer: Discoverer,
        enabler: DisabledEntityEnabler,
        telemetry: TelemetryPublisher,
        dashboard: DashboardProvisioner,
        health: HealthReporter,
    ) -> None:
        """Args:
            ha: HA client, for the registries.
            discoverer: Entity discovery.
            enabler: Disabled-entity pass.
            telemetry: Gets each discovery result and the firmware map.
            dashboard: Gets each discovery result.
            health: Counts reconnects.
        """
        self._ha = ha
        self._discoverer = discoverer
        self._enabler = enabler
        self._telemetry = telemetry
        self._dashboard = dashboard
        self._health = health
        self._run_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending_devices: Set[str] = set()
        self._debounce: Optional[threading.Timer] = None

    def on_reconnect(self) -> None:
        """Runs on the listener thread, so discovery is done before events
        are dispatched."""
        self._health.record_reconnect()
        self._run(only_device_ids=None)

    def on_device_registry_updated(self, data: Dict[str, Any]) -> None:
        """Queues the device and restarts the debounce timer. Removals are ignored.

        Args:
            data: Event data with "action" and "device_id".
        """
        if data.get("action") not in ("create", "update") or not data.get("device_id"):
            return
        with self._pending_lock:
            self._pending_devices.add(data["device_id"])
            if self._debounce is not None:
                self._debounce.cancel()
            self._debounce = threading.Timer(DEVICE_EVENT_DEBOUNCE_S, self._flush_device_events)
            self._debounce.daemon = True
            self._debounce.start()

    def _flush_device_events(self) -> None:
        with self._pending_lock:
            devices = self._pending_devices
            self._pending_devices = set()
            self._debounce = None
        if devices:
            logger.info("Device registry changed (%s devices), rediscovering", len(devices))
            self._run(only_device_ids=devices)

    def _run(self, only_device_ids: Optional[Set[str]]) -> None:
        """Enable pass, then discovery, telemetry and dashboard. If the
        registries can't be read, discovery still runs.

        Args:
            only_device_ids: Limit the enable pass to these devices.
        """
        with self._run_lock:
            enabled = 0
            try:
                snapshot = RegistrySnapshot.fetch(self._ha)
                enabled = self._enabler.run(snapshot, self._discoverer.seed_entity_ids(), only_device_ids)
                self._telemetry.set_firmware(snapshot.firmware_by_entity())
            except Exception:  # noqa: BLE001
                logger.warning("Could not read HA registries, skipping enable pass", exc_info=True)

            discovery = self._discoverer.discover_all()
            self._telemetry.on_rediscover(discovery)
            # Dashboard writes are slow, keep them off this thread.
            threading.Thread(target=self._dashboard.update_discovery, args=(discovery,), daemon=True).start()

        if enabled:
            logger.info("Enabled %s entities, rediscovering in %ss", enabled, FOLLOW_UP_DELAY_S)
            timer = threading.Timer(FOLLOW_UP_DELAY_S, self._run, kwargs={"only_device_ids": None})
            timer.daemon = True
            timer.start()

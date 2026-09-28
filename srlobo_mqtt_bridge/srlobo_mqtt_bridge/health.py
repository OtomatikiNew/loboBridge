"""Bridge health telemetry, ADR-018 Decision 1 (accepted fields only).

Skips continuous CPU/RAM/log streams on purpose (ADR-018 says none of that
is actionable at 2,000+ installations, pull it on demand instead). Also
doesn't attempt HOST_RESOURCE_LOW incident detection since that has no
defined bridge-side signal given the same ADR rejects continuous resource
telemetry. Left open until that gap gets resolved.

Doesn't try to force clock sync either (ADR-018 rejects that too, HAOS
already runs systemd-timesyncd on its own), only detects and reports it.
"""

import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Deque, Dict, Optional

import requests

from . import __version__
from .config import AddonOptions
from .ha_client import HomeAssistantClient
from .mqtt_client import BridgeMqttClient

logger = logging.getLogger(__name__)

PUBLISH_INTERVAL_S = 60
CLOCK_CHECK_TIMEOUT_S = 10
CLOCK_DRIFT_THRESHOLD_S = 60
RESTART_WINDOW_S = 24 * 60 * 60


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class HealthReporter:
    def __init__(self, mqtt: BridgeMqttClient, ha: HomeAssistantClient, options: AddonOptions) -> None:
        self._mqtt = mqtt
        self._ha = ha
        self._options = options
        self._start_time = time.monotonic()
        # Approximates Core instability via HA WebSocket reconnects. A plain
        # network blip also increments this, so it's not a precise restart
        # counter, just good enough for the fleet-wide "which installations
        # are flapping" signal ADR-018 wants. A real restart-vs-network-blip
        # split would need the two-signal probe from lessons-from-lobobrain
        # #4, not doing that here.
        self._reconnect_timestamps: Deque[float] = deque()
        self._last_backup_at: Optional[str] = None
        self._stop = threading.Event()

    def record_reconnect(self) -> None:
        self._reconnect_timestamps.append(time.monotonic())

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._publish_once()
            except Exception:
                logger.exception("Failed to publish bridge/status")
            self._stop.wait(PUBLISH_INTERVAL_S)

    def _publish_once(self) -> None:
        now = time.monotonic()
        while self._reconnect_timestamps and now - self._reconnect_timestamps[0] > RESTART_WINDOW_S:
            self._reconnect_timestamps.popleft()

        payload: Dict[str, Any] = {
            "online": True,
            "version": __version__,
            "uptime_s": int(time.monotonic() - self._start_time),
            "ha_core_restarts_24h": len(self._reconnect_timestamps),
            "clock_synced": self._check_clock_synced(),
            "updated_at": _utc_now_iso(),
        }

        core_version = self._get_core_version()
        if core_version:
            payload["ha_core_version"] = core_version

        supervisor_version, last_backup_at = self._get_supervisor_info()
        if supervisor_version:
            payload["supervisor_version"] = supervisor_version
        if last_backup_at:
            payload["last_backup_at"] = last_backup_at

        self._mqtt.publish_bridge_status(payload)

    def _get_core_version(self) -> Optional[str]:
        try:
            info = self._ha.get_core_info()
            version = info.get("version")
            return str(version) if version else None
        except Exception:
            logger.warning("Could not read HA core version for bridge/status", exc_info=True)
            return None

    def _get_supervisor_info(self) -> "tuple[Optional[str], Optional[str]]":
        try:
            info = self._ha.get_supervisor_info()
            data = info.get("data", info)  # Supervisor API wraps results under "data"
            return data.get("version"), self._last_backup_at
        except Exception:
            logger.warning("Could not read Supervisor info for bridge/status", exc_info=True)
            return None, self._last_backup_at

    def _check_clock_synced(self) -> Optional[bool]:
        """Compares local clock against the Date header of an SR.Lobo Cloud
        response. Cheap way to detect drift without needing host systemd
        access from inside the add-on container."""
        try:
            response = requests.head(self._options.srlobo_api_url, timeout=CLOCK_CHECK_TIMEOUT_S)
            server_date = response.headers.get("Date")
            if not server_date:
                return None
            remote_time = parsedate_to_datetime(server_date)
            drift = abs((datetime.now(timezone.utc) - remote_time).total_seconds())
            return drift < CLOCK_DRIFT_THRESHOLD_S
        except Exception:
            logger.warning("Could not check clock sync for bridge/status", exc_info=True)
            return None

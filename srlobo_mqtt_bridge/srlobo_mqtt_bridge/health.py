"""Bridge health telemetry (accepted fields only).

Skips continuous CPU/RAM/log streams on purpose: none of that is actionable
at fleet scale, pull it on demand instead. Doesn't try to force clock sync
either, since HAOS already runs systemd-timesyncd on its own; only detects
and reports drift.
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
    """Returns the current UTC time formatted as an ISO-8601 string.

    Returns:
        The current UTC timestamp, e.g. "2026-01-01T00:00:00Z".
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class HealthReporter:
    """Periodically publishes bridge/status health telemetry over MQTT."""

    def __init__(self, mqtt: BridgeMqttClient, ha: HomeAssistantClient, options: AddonOptions) -> None:
        """Stores the collaborators needed to build and publish health
        status, and initializes reconnect tracking.

        Args:
            mqtt: Client used to publish bridge/status.
            ha: Client used to read HA core/supervisor version info.
            options: Add-on options, used to reach the clock-check endpoint.
        """
        self._mqtt = mqtt
        self._ha = ha
        self._options = options
        self._start_time = time.monotonic()
        # Approximates Core instability via HA WebSocket reconnects. A plain
        # network blip also increments this, so it's not a precise restart
        # counter, just good enough for a fleet-wide "which installations
        # are flapping" signal.
        self._reconnect_timestamps: Deque[float] = deque()
        self._last_backup_at: Optional[str] = None
        self._stop = threading.Event()

    def record_reconnect(self) -> None:
        """Records a WebSocket (re)connect for the 24h restart-count metric."""
        self._reconnect_timestamps.append(time.monotonic())

    def start(self) -> None:
        """Starts the background thread that publishes status periodically."""
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        """Signals the background publish loop to stop."""
        self._stop.set()

    def _loop(self) -> None:
        """Publishes a health snapshot on a fixed interval until stopped."""
        while not self._stop.is_set():
            try:
                self._publish_once()
            except Exception:
                logger.exception("Failed to publish bridge/status")
            self._stop.wait(PUBLISH_INTERVAL_S)

    def _publish_once(self) -> None:
        """Builds and publishes a single bridge/status health payload."""
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
        """Reads the HA Core version, if available.

        Returns:
            The core version string, or None if it could not be read.
        """
        try:
            info = self._ha.get_core_info()
            version = info.get("version")
            return str(version) if version else None
        except Exception:
            logger.warning("Could not read HA core version for bridge/status", exc_info=True)
            return None

    def _get_supervisor_info(self) -> "tuple[Optional[str], Optional[str]]":
        """Reads the Supervisor version and last known backup time.

        Returns:
            A (supervisor_version, last_backup_at) tuple; either element may be None.
        """
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
        access from inside the add-on container.

        Returns:
            True if clock drift is within the acceptable threshold, False
            if it exceeds it, or None if the check could not be performed.
        """
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

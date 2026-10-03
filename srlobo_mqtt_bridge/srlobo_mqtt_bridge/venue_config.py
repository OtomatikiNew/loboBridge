"""Venue configuration via retained MQTT.

Bootstrap stays call-once for connection-critical config. This handles venue
metadata that can change mid-session (club rename, address correction,
provider switch, court rename) without a restart. Retained, so it fires
immediately on subscribe, same effect as bootstrap for a fresh install.
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .ha_client import HomeAssistantClient, HomeAssistantError

logger = logging.getLogger(__name__)

SUPPORTED_SCHEMA_VERSION = 1
_REQUIRED_FIELDS = ("club_name", "latitude", "longitude", "timezone", "language")


@dataclass
class VenueConfig:
    schema_version: int
    club_name: str
    address: Optional[str]
    latitude: float
    longitude: float
    timezone: str
    language: str
    booking_system: Optional[str]
    court_names: Dict[int, str]


class VenueConfigHandler:
    """Validates and applies venue/config MQTT messages."""

    def __init__(self, ha: HomeAssistantClient) -> None:
        """Stores the HA client used to apply venue config to HA core.

        Args:
            ha: Client used to write venue latitude/longitude/timezone/language to HA core config.
        """
        self._ha = ha
        self._current: Optional[VenueConfig] = None

    @property
    def current(self) -> Optional[VenueConfig]:
        """Returns:
            The most recently applied venue config, or None if none has
            been received yet.
        """
        return self._current

    def handle(self, payload: Dict[str, Any]) -> None:
        """Validates an incoming venue/config payload and, if valid,
        applies it as the current venue config and pushes location/
        timezone/language to HA core config.

        Args:
            payload: Decoded venue/config message payload.
        """
        parsed = self._validate(payload)
        if parsed is None:
            logger.error("Rejected invalid venue/config snapshot, keeping previous one")
            return

        self._current = parsed
        logger.info("Applied venue config for %s", parsed.club_name)

        # Writing this to HA core config removes a manual per-install step
        # (lat/long/tz/language feed HA's own sun integration, weather,
        # local automations).
        try:
            self._ha.set_core_config(
                latitude=parsed.latitude,
                longitude=parsed.longitude,
                time_zone=parsed.timezone,
                language=parsed.language,
            )
        except HomeAssistantError:
            logger.exception("Failed to apply venue config to HA core config")

    def _validate(self, payload: Dict[str, Any]) -> Optional[VenueConfig]:
        """Validates a venue/config payload's schema version and required
        fields, and parses it into a VenueConfig.

        Args:
            payload: Decoded venue/config message payload.

        Returns:
            The parsed VenueConfig, or None if the payload is invalid.
        """
        if payload.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
            logger.error("Unsupported venue/config schema_version: %r", payload.get("schema_version"))
            return None
        for field_name in _REQUIRED_FIELDS:
            if payload.get(field_name) in (None, ""):
                logger.error("venue/config missing required field %r", field_name)
                return None
        try:
            latitude = float(payload["latitude"])
            longitude = float(payload["longitude"])
        except (TypeError, ValueError):
            logger.error("venue/config latitude/longitude are not numeric")
            return None

        courts_raw = payload.get("courts", {})
        court_names: Dict[int, str] = {}
        for index_str, court in courts_raw.items():
            try:
                court_names[int(index_str)] = court["name"]
            except (TypeError, ValueError, KeyError):
                logger.warning("Ignoring malformed court entry in venue/config: %r", court)

        return VenueConfig(
            schema_version=payload["schema_version"],
            club_name=payload["club_name"],
            address=payload.get("address"),
            latitude=latitude,
            longitude=longitude,
            timezone=payload["timezone"],
            language=payload["language"],
            booking_system=payload.get("booking_system"),
            court_names=court_names,
        )

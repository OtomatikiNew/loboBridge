"""SrLobo Cloud bootstrap client. One-time call at startup to fetch
per-installation MQTT credentials and entity config."""

import logging
import time
from typing import Any, Dict, Optional

import requests

from .config import BootstrapConfig, CourtEntity, DoorEntity, MqttConfig
from .logging_setup import redact

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_S = 20
MAX_ATTEMPTS = 5
RETRY_BASE_DELAY_S = 2
RETRY_MAX_DELAY_S = 30


class BootstrapError(RuntimeError):
    """Raised when the bootstrap call fails or returns an unusable response."""


class SrLoboBootstrapClient:
    """Client for the one-time SrLobo Cloud bootstrap call that supplies
    MQTT credentials and installation entity config."""

    def __init__(self, api_url: str, bootstrap_path: str, token: str) -> None:
        """Stores the bootstrap endpoint and auth token for later use.

        Args:
            api_url: Base URL of the SrLobo Cloud API.
            bootstrap_path: Path of the bootstrap endpoint, appended to api_url.
            token: Bearer token used to authenticate the bootstrap request.
        """
        self.api_url = api_url.rstrip("/")
        self.bootstrap_path = bootstrap_path
        self.token = token

    def fetch(self) -> BootstrapConfig:
        """Fetches and parses the bootstrap response, retrying with backoff
        on transient failures. Never logs the token or raw response, only
        the redacted form at debug level.

        Returns:
            The parsed bootstrap configuration.

        Raises:
            BootstrapError: If every retry attempt fails.
        """
        last_error: Optional[Exception] = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return self._fetch_once()
            except (requests.RequestException, BootstrapError) as exc:
                last_error = exc
                if attempt == MAX_ATTEMPTS:
                    break
                delay = min(RETRY_BASE_DELAY_S * 2 ** (attempt - 1), RETRY_MAX_DELAY_S)
                logger.warning(
                    "Bootstrap attempt %s/%s failed (%s), retrying in %ss",
                    attempt,
                    MAX_ATTEMPTS,
                    exc,
                    delay,
                )
                time.sleep(delay)
        raise BootstrapError(f"Bootstrap failed after {MAX_ATTEMPTS} attempts") from last_error

    def _fetch_once(self) -> BootstrapConfig:
        """Performs a single bootstrap HTTP request and parses the result.

        Returns:
            The parsed bootstrap configuration.

        Raises:
            requests.RequestException: If the HTTP request itself fails.
            BootstrapError: If the response is missing required fields.
        """
        url = f"{self.api_url}{self.bootstrap_path}"
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_S)
        response.raise_for_status()
        payload = response.json()
        logger.debug("Bootstrap response: %s", redact(payload))
        return self._parse(payload)

    def _parse(self, payload: Dict[str, Any]) -> BootstrapConfig:
        """Converts the raw bootstrap JSON payload into a BootstrapConfig.

        Args:
            payload: Parsed JSON body of the bootstrap response.

        Returns:
            The structured bootstrap configuration.

        Raises:
            BootstrapError: If a required field is missing from the payload.
        """
        mqtt_payload = payload.get("mqtt", {})
        try:
            mqtt_config = MqttConfig(
                broker=mqtt_payload["broker"],
                port=int(mqtt_payload.get("port", 8883)),
                username=mqtt_payload.get("username"),
                password=mqtt_payload.get("password"),
                client_id=mqtt_payload.get("client_id"),
                base_topic=mqtt_payload["base_topic"].strip("/"),
                tls=bool(mqtt_payload.get("tls", True)),
                ca_cert=mqtt_payload.get("ca_cert"),
                client_cert=mqtt_payload.get("client_cert"),
                client_key=mqtt_payload.get("client_key"),
            )
        except KeyError as exc:
            raise BootstrapError(f"Bootstrap response missing required mqtt field: {exc}") from exc

        club = payload.get("club", {})
        source_system = club.get("source_system") or payload.get("source_system")
        installation_id = (
            payload.get("installation_id") or club.get("uuid") or mqtt_config.base_topic.split("/")[-1]
        )
        if not installation_id:
            raise BootstrapError("Bootstrap response has no installation_id")

        courts = [self._parse_court(item, source_system) for item in payload.get("courts", [])]
        doors = [self._parse_door(item, source_system) for item in payload.get("doors", [])]

        return BootstrapConfig(
            installation_id=installation_id,
            source_system=source_system,
            mqtt=mqtt_config,
            courts=courts,
            doors=doors,
        )

    @staticmethod
    def _parse_court(item: Dict[str, Any], source_system: Optional[str]) -> CourtEntity:
        """Builds a CourtEntity from one item of the bootstrap `courts` list.

        Args:
            item: Raw court entry from the bootstrap payload.
            source_system: Fallback source system if the item doesn't specify its own.

        Returns:
            The parsed court entity.
        """
        return CourtEntity(
            index=int(item["index"]),
            source_id=str(item.get("source_id") or item.get("id") or "") or None,
            source_system=item.get("source_system") or source_system,
        )

    @staticmethod
    def _parse_door(item: Dict[str, Any], source_system: Optional[str]) -> DoorEntity:
        """Builds a DoorEntity from one item of the bootstrap `doors` list.

        Args:
            item: Raw door entry from the bootstrap payload.
            source_system: Fallback source system if the item doesn't specify its own.

        Returns:
            The parsed door entity.
        """
        return DoorEntity(
            index=int(item["index"]),
            entity_id=item.get("entity_id"),
            source_id=str(item.get("source_id") or item.get("id") or "") or None,
            source_system=item.get("source_system") or source_system,
        )

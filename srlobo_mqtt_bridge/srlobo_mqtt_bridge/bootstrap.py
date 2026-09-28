"""
SrLobo Cloud bootstrap client. 
One-time call at startup to get connected,
"""

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
    pass


class SrLoboBootstrapClient:
    def __init__(self, api_url: str, bootstrap_path: str, token: str) -> None:
        self.api_url = api_url.rstrip("/")
        self.bootstrap_path = bootstrap_path
        self.token = token

    def fetch(self) -> BootstrapConfig:
        """
        Fetches and parses the bootstrap response, 
        retrying with backoff on transient failures. 
        Never logs the token or raw response, 
        only the redacted form at debug level.
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
        url = f"{self.api_url}{self.bootstrap_path}"
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_S)
        response.raise_for_status()
        payload = response.json()
        logger.debug("Bootstrap response: %s", redact(payload))
        return self._parse(payload)

    def _parse(self, payload: Dict[str, Any]) -> BootstrapConfig:
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
        return CourtEntity(
            index=int(item["index"]),
            source_id=str(item.get("source_id") or item.get("id") or "") or None,
            source_system=item.get("source_system") or source_system,
        )

    @staticmethod
    def _parse_door(item: Dict[str, Any], source_system: Optional[str]) -> DoorEntity:
        return DoorEntity(
            index=int(item["index"]),
            entity_id=item.get("entity_id"),
            source_id=str(item.get("source_id") or item.get("id") or "") or None,
            source_system=item.get("source_system") or source_system,
        )

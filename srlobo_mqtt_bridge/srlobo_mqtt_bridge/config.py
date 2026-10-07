"""Add-on options loading and the config shapes used throughout the bridge."""

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

OPTIONS_PATH = "/data/options.json"
DEFAULT_REENABLE_MANUFACTURERS = ("Shelly", "Nuki")


@dataclass
class MqttConfig:
    """Per-installation MQTT connection details, sourced from bootstrap.
    Never hardcode this, never share it across clubs."""

    broker: str
    port: int = 8883
    username: Optional[str] = None
    password: Optional[str] = None
    client_id: Optional[str] = None
    base_topic: str = ""
    tls: bool = True
    # X.509 fallback, only used if backend sends certs instead of
    # username/password.
    ca_cert: Optional[str] = None
    client_cert: Optional[str] = None
    client_key: Optional[str] = None


@dataclass
class CourtEntity:
    """A court from the bootstrap response. Real HA entity comes from `index`
    (`light.luces_padel_{index}`), not from a bootstrap name."""

    index: int
    source_id: Optional[str] = None
    source_system: Optional[str] = None


@dataclass
class DoorEntity:
    """A door from the bootstrap response.

    No fixed naming convention exists for doors like courts have, so
    `entity_id` is taken as the real HA lock entity for this door
    (e.g. a Nuki `lock.*`).
    """

    index: int
    entity_id: Optional[str] = None
    source_id: Optional[str] = None
    source_system: Optional[str] = None


@dataclass
class BootstrapConfig:
    installation_id: str
    source_system: Optional[str]
    mqtt: MqttConfig
    courts: List[CourtEntity] = field(default_factory=list)
    doors: List[DoorEntity] = field(default_factory=list)


@dataclass
class AddonOptions:
    srlobo_token: str
    srlobo_api_url: str
    bootstrap_path: str
    log_level: str
    # No confirmed naming convention for these entities, so they're
    # configurable instead of hardcoded. {n} = court number, from 1.
    mode_select_entity_template: str = "input_boolean.regulacion_por_lux_pista_{n}"
    lux_reference_entity_template: str = "input_number.referencia_de_lux_pista_{n}"
    calibration_trigger_entity_template: str = "input_button.fijar_referencia_pista_{n}"
    court_signal_entity_template: str = "binary_sensor.pista_{n}"
    local_auto_manual_entity_template: str = "input_boolean.auto_manual_luz_{n}"
    # Devices from these manufacturers get their disabled entities
    # re-enabled (device_registry.py). Substring match, case-insensitive.
    reenable_manufacturers: List[str] = field(default_factory=lambda: list(DEFAULT_REENABLE_MANUFACTURERS))


def load_options() -> AddonOptions:
    """Loads and validates the add-on's options.json, applying defaults for
    anything not explicitly set.

    Returns:
        The parsed add-on options.

    Raises:
        RuntimeError: If the required `srlobo_token` option is missing.
    """
    with open(OPTIONS_PATH, "r", encoding="utf-8") as fh:
        raw: Dict[str, Any] = json.load(fh)

    token = raw.get("srlobo_token")
    if not token:
        raise RuntimeError("Missing required option: srlobo_token")

    return AddonOptions(
        srlobo_token=token,
        srlobo_api_url=raw.get("srlobo_api_url", "https://srlobo.otomatiki.xyz"),
        bootstrap_path=raw.get("bootstrap_path", "/api/homeassistant/bootstrap"),
        log_level=raw.get("log_level", "info"),
        mode_select_entity_template=raw.get(
            "mode_select_entity_template", "input_boolean.regulacion_por_lux_pista_{n}"
        ),
        lux_reference_entity_template=raw.get(
            "lux_reference_entity_template", "input_number.referencia_de_lux_pista_{n}"
        ),
        calibration_trigger_entity_template=raw.get(
            "calibration_trigger_entity_template", "input_button.fijar_referencia_pista_{n}"
        ),
        court_signal_entity_template=raw.get("court_signal_entity_template", "binary_sensor.pista_{n}"),
        local_auto_manual_entity_template=raw.get(
            "local_auto_manual_entity_template", "input_boolean.auto_manual_luz_{n}"
        ),
        reenable_manufacturers=list(raw.get("reenable_manufacturers", DEFAULT_REENABLE_MANUFACTURERS)),
    )

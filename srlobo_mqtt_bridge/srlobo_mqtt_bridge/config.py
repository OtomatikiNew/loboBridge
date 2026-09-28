"""Add-on options loading and the config shapes used throughout the bridge."""

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

OPTIONS_PATH = "/data/options.json"


@dataclass
class MqttConfig:
    """Per-installation MQTT connection details, sourced from bootstrap.
    Never hardcode this, never share it across clubs (ADR-017)."""

    broker: str
    port: int = 8883
    username: Optional[str] = None
    password: Optional[str] = None
    client_id: Optional[str] = None
    base_topic: str = ""
    tls: bool = True
    # X.509 fallback (ADR-017), only used if backend sends certs instead of
    # username/password.
    ca_cert: Optional[str] = None
    client_cert: Optional[str] = None
    client_key: Optional[str] = None


@dataclass
class CourtEntity:
    """A court from the bootstrap response. Real HA entity comes from `index`
    (`light.luces_padel_{index}`, ADR-007 §1), not from a bootstrap name."""

    index: int
    source_id: Optional[str] = None
    source_system: Optional[str] = None


@dataclass
class DoorEntity:
    """A door from the bootstrap response.

    No fixed naming convention exists for doors like courts have
    (ADR-019 doesn't define one), so `entity_id` is taken as the real HA
    lock entity for this door (e.g. a Nuki `lock.*`). Note this is a
    different meaning than the old add-on used this field for. Worth
    double-checking with Álvaro that this matches what installers set up.
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
    # Mode select / calibration button / lux reference entities don't have a
    # confirmed naming convention anywhere yet, so these are configurable
    # instead of hardcoded. {n} = court's 1-based index. Placeholder defaults
    # for now, need to confirm real names against the HA blueprint before
    # trusting mode-switching/calibration in production.
    mode_select_entity_template: str = "input_select.modo_pista_{n}"
    lux_reference_entity_template: str = "input_number.referencia_lux_pista_{n}"
    calibration_trigger_entity_template: str = "input_button.calibrar_pista_{n}"


def load_options() -> AddonOptions:
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
            "mode_select_entity_template", "input_select.modo_pista_{n}"
        ),
        lux_reference_entity_template=raw.get(
            "lux_reference_entity_template", "input_number.referencia_lux_pista_{n}"
        ),
        calibration_trigger_entity_template=raw.get(
            "calibration_trigger_entity_template", "input_button.calibrar_pista_{n}"
        ),
    )

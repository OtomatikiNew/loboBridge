# SrLobo MQTT Bridge

This Home Assistant add-on connects Home Assistant to SrLobo Cloud through a normalized MQTT contract.

## Configuration

Only one mandatory option is required:

```yaml
srlobo_token: "..."
```

Optional options:

```yaml
srlobo_api_url: "https://srlobo.otomatiki.xyz"
bootstrap_path: "/api/homeassistant/bootstrap"
log_level: "info"
```

## Entity model

The add-on creates stable binary sensors in Home Assistant:

```text
binary_sensor.pista_1
binary_sensor.pista_2
binary_sensor.puerta_1
binary_sensor.puerta_2
```

The visible `friendly_name` is provided by SrLobo Cloud and is not derived from the entity ID.

## MQTT model

The add-on expects normalized topics such as:

```text
srlobo/{installation_id}/courts/{index}/state
srlobo/{installation_id}/doors/{index}/state
srlobo/{installation_id}/status
```

See the repository `docs/` folder for the full MQTT contract.

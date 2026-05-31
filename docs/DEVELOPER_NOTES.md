# Developer notes

## What this add-on is

This add-on is a reference implementation of the proposed SrLobo MQTT abstraction layer.

It is intentionally simple:

1. Read a SrLobo token from add-on options.
2. Call SrLobo Cloud bootstrap endpoint.
3. Connect to MQTT using the broker details returned by SrLobo.
4. Create stable Home Assistant binary sensors.
5. Update those sensors when normalized MQTT state messages arrive.

## What this add-on should not do

It should not contain provider-specific logic:

- no Playtomic branches
- no Syltek branches
- no Taykus branches
- no local-mode special cases
- no provider-specific MQTT topics
- no manual club ID / facility ID configuration

All provider-specific logic should live in SrLobo Cloud.

## Home Assistant API authentication

The old add-on required a long-lived Home Assistant token in the options.

This version uses Home Assistant add-on internal API access instead:

```yaml
homeassistant_api: true
```

The add-on then uses the `SUPERVISOR_TOKEN` environment variable to call:

```text
http://supervisor/core/api/states/<entity_id>
```

This removes the need for the installer to create and paste a Home Assistant long-lived token.

## Current implementation status

This code is a base implementation. It is suitable for integration work but still needs to be wired to the real SrLobo bootstrap endpoint and tested in a real Home Assistant add-on environment.

Implemented:

- token-based bootstrap call
- MQTT connection based on bootstrap response
- stable court binary sensors
- stable door binary sensors
- court state updates
- door state updates
- club status binary sensor
- add-on availability heartbeat

Not yet implemented:

- Home Assistant state-change listener for publishing local commands back to SrLobo
- creation of helper entities such as timers, input booleans or light groups
- Home Assistant MQTT Discovery mode
- advanced reconnection/backoff strategy
- runtime config reload from retained MQTT config

## Recommended next steps

1. Implement the SrLobo bootstrap endpoint.
2. Publish retained MQTT state messages for each court and door.
3. Test that Home Assistant creates:
   - `binary_sensor.pista_1`
   - `binary_sensor.pista_2`
   - `binary_sensor.puerta_1`
4. Add optional Home Assistant WebSocket listener if bidirectional local command sync is needed.
5. Add unit tests for bootstrap parsing and MQTT payload handling.

## Why this is still useful with the existing codebase

The current add-on already has useful parts:

- MQTT client usage
- Home Assistant entity updates
- dashboard logic
- local control concepts
- brightness, automatic mode, min/max level, limited mode

This new add-on does not discard that knowledge. It reorganizes the architecture so those concepts are exposed through a single normalized MQTT contract.


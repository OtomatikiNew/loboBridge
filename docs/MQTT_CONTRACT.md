# SrLobo MQTT Contract for Home Assistant

## Goal

The MQTT layer must be the stable abstraction between SrLobo Cloud and Home Assistant.

The Home Assistant add-on must not know whether the original data source is Syltek, Playtomic, Taykus, or a local integration. SrLobo Cloud must normalize all provider-specific differences before publishing MQTT messages.

## Main principle

Provider-specific systems:

```text
Syltek / Playtomic / Taykus / Local
```

must be normalized by SrLobo Cloud before reaching MQTT:

```text
External systems -> SrLobo Cloud -> Standard MQTT -> Home Assistant add-on
```

The add-on receives only stable entities such as:

```text
binary_sensor.pista_1
binary_sensor.pista_2
binary_sensor.puerta_1
binary_sensor.puerta_2
```

The original provider IDs may exist inside SrLobo Cloud, but they should not be required by Home Assistant.

## Bootstrap

The add-on should only require one option:

```yaml
srlobo_token: "..."
```

At startup it calls:

```http
GET /api/homeassistant/bootstrap
Authorization: Bearer <srlobo_token>
```

The response must include everything needed by the add-on:

- club / installation identity
- MQTT broker details
- MQTT base topic
- court list
- door list
- friendly names
- stable entity names

### Bootstrap response example

```json
{
  "installation_id": "club_109",
  "club": {
    "uuid": "club_109",
    "name": "Example Club",
    "timezone": "Europe/Madrid",
    "source_system": "playtomic"
  },
  "mqtt": {
    "broker": "mqtt.srlobo.example",
    "port": 8883,
    "tls": true,
    "username": "mqtt-user",
    "password": "mqtt-password",
    "client_id": "ha-addon-club-109",
    "base_topic": "srlobo/club_109"
  },
  "courts": [
    {
      "index": 1,
      "entity_id": "pista_1",
      "friendly_name": "PISTA CENTRAL G24",
      "device_class": "light",
      "source_id": "72",
      "source_system": "playtomic"
    },
    {
      "index": 2,
      "entity_id": "pista_2",
      "friendly_name": "PISTA 2",
      "device_class": "light",
      "source_id": "73",
      "source_system": "playtomic"
    }
  ],
  "doors": [
    {
      "index": 1,
      "entity_id": "puerta_1",
      "friendly_name": "Puerta principal",
      "device_class": "door",
      "source_id": "41",
      "source_system": "playtomic"
    }
  ]
}
```

`source_system` is informational only. The add-on must not branch its runtime behavior based on it.

## MQTT base topic

Recommended format:

```text
srlobo/{installation_id}
```

Example:

```text
srlobo/club_109
```

## Topics

### Add-on availability

Published by the add-on:

```text
srlobo/{installation_id}/addon/availability
```

Payload:

```json
{
  "addon_online": true,
  "timestamp": "2026-05-27T12:00:00Z"
}
```

This topic should be retained.

### Club status

Published by SrLobo Cloud:

```text
srlobo/{installation_id}/status
```

Payload:

```json
{
  "online": true,
  "active_reservations": 4,
  "manual_courts": 1,
  "doors_open": 0,
  "updated_at": "2026-05-27T12:00:00Z"
}
```

### Court state

Published by SrLobo Cloud:

```text
srlobo/{installation_id}/courts/{court_index}/state
```

Example:

```text
srlobo/club_109/courts/1/state
```

Payload:

```json
{
  "state": "on",
  "brightness_pct": 50,
  "reservation_active": true,
  "occupied": true,
  "automatic_mode": true,
  "manual_mode": false,
  "min_level": 0,
  "max_level": 50,
  "limited_mode": true,
  "online": true,
  "updated_at": "2026-05-27T12:00:00Z"
}
```

This single topic replaces the current scattered concepts:

- reservation state
- light state
- brightness
- automatic mode
- minimum level
- maximum level
- limited mode

### Court command

Published by the add-on or by SrLobo UI when Home Assistant should perform or reflect a command:

```text
srlobo/{installation_id}/courts/{court_index}/command
```

Payload:

```json
{
  "state": "on",
  "brightness_pct": 70,
  "automatic_mode": false,
  "manual_timeout_min": 30
}
```

### Door state

Published by SrLobo Cloud:

```text
srlobo/{installation_id}/doors/{door_index}/state
```

Payload:

```json
{
  "state": "closed",
  "open": false,
  "locked": true,
  "automatic_mode": true,
  "online": true,
  "updated_at": "2026-05-27T12:00:00Z"
}
```

### Door command

Published when a door action is requested:

```text
srlobo/{installation_id}/doors/{door_index}/command
```

Payload:

```json
{
  "action": "open"
}
```

Supported actions should be defined by SrLobo Cloud, for example:

```text
open, close, lock, unlock, pulse
```

## Mapping from current topics

The existing add-on currently uses different topics depending on source system. The new MQTT contract should cover all of them through the normalized topics above.

Current concepts to preserve:

| Current concept | New normalized topic |
|---|---|
| `ok_cloud/reservation/playtomic/{club_id}` | `srlobo/{installation_id}/courts/{index}/state` |
| `ok_cloud/reservation/{club_id}/{facility_id}/{light_id}` | `srlobo/{installation_id}/courts/{index}/state` |
| `ok_cloud/light/automatic/...` | field `automatic_mode` in court state |
| `ok_cloud/light/min_level/...` | field `min_level` in court state |
| `ok_cloud/light/max_level/...` | field `max_level` in court state |
| `ok_cloud/light/limited_mode/...` | field `limited_mode` in court state |
| `ok_cloud/door/...` | `srlobo/{installation_id}/doors/{index}/state` |
| `ok_cloud/door/automatic/...` | field `automatic_mode` in door state |

## Entity naming

SrLobo Cloud should send stable entity names directly in the bootstrap response.

Court entities:

```text
binary_sensor.pista_1
binary_sensor.pista_2
binary_sensor.pista_3
```

Door entities:

```text
binary_sensor.puerta_1
binary_sensor.puerta_2
binary_sensor.puerta_3
```

The `friendly_name` must remain the real display name of the court or door.

Example:

```text
entity_id: binary_sensor.pista_1
friendly_name: PISTA CENTRAL G24
```


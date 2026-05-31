# SrLobo MQTT Bridge for Home Assistant

This repository contains a reference Home Assistant add-on that implements a clean MQTT abstraction layer between SrLobo Cloud and Home Assistant.

The add-on is designed to replace provider-specific logic inside Home Assistant. SrLobo Cloud is responsible for normalizing data from Syltek, Playtomic, Taykus, or local systems before publishing MQTT messages.

The add-on only needs a SrLobo installation token. It then obtains its bootstrap configuration from SrLobo Cloud, connects to the MQTT broker, creates stable Home Assistant entities, and keeps them updated.

See `docs/MQTT_CONTRACT.md` and `docs/DEVELOPER_NOTES.md` for the full specification.

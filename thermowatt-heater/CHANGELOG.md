# Changelog

## 1.3.6

- Add readable device-error status and explanation sensors using the existing status poll.
- Identify the high ambient temperature heating pause observed on thermostat firmware 07.00.09.06, with automatic recovery guidance.
- Preserve raw flags and report unmapped or missing errors without guessing their meaning on other firmware.
- Log device-error changes and recovery once per transition; leave heater protection, operating mode and polling unchanged.

## 1.3.5

- Fix the Anti-Legionella Active indicator using the heater status flag; the mapping is inferred from firmware telemetry and needs validation on other firmware.
- Keep operating mode and heating indicators independent of the sanitation-cycle flag.
- Preserve existing entities and show unknown for older data missing the cycle flag.
- Show concise release notes in the Home Assistant app update dialog.

## 1.3.4

- Add UTC timestamps, severity, request status and duration to bridge logs.
- Report availability changes and recovery duration.
- Remove account email, credentials and raw request details from log messages.
- Count valid status updates accurately in polling summaries.

## 1.3.3

- Add bridge state, heartbeat, cooldown reason and next-retry diagnostic sensors.
- Keep diagnostics available during cloud pauses without making extra cloud requests.
- Report saved cooldown deadlines in logs and preserve them across upgrades.

## 1.3.2

- Fix installation with current Supervisor builds by setting an explicit base image.
- Support amd64 and aarch64 architectures.
- Add container build and startup checks.

## 1.3.1

- Use configurable five-minute polling and preserve rate-limit cooldowns across restarts.
- Reject failed cloud responses and invalid temperature readings.
- Queue commands, ignore retained commands and wait for genuine status updates.
- Preserve existing entities and add regression checks.

## 1.3.0

- Replace AWS MQTT status updates with cloud polling.
- Remove certificate-based AWS IoT authentication.

## 1.2.0

- Add AWS MQTT status updates and support for multiple devices.
- Add certificate-based AWS IoT authentication.
- Send commands through the REST API.
- Add command cooldown handling to prevent stale status updates.

## 1.1.1

- Set the polling interval to 60 seconds.
- Update operating-mode names to match the vendor app.

## 1.1.0

- Update compatibility with backend changes introduced with vendor app version 3.14.

## 1.0.0

- Add cloud connectivity, temperature monitoring and mode control.
- Add MQTT device discovery in Home Assistant.


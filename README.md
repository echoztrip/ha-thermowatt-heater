# Thermowatt Smart Boiler Bridge for Home Assistant

This fork includes a reliability repair in version 1.3.1. See [repair details,
installation, validation and rollback](REPAIR.md) before switching an existing system.

This add-on allows you to integrate Thermowatt-based smart water heaters into Home Assistant using MQTT. It bridges the gap between the Thermowatt cloud and your local Home Assistant instance.

[![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fwaterheater-dev%2Fha-thermowatt-heater)

## Features

- **Real-time Monitoring**: Track current water temperature (`T_Avg`).
- **Full Control**: Set target temperatures and toggle between Manual/Auto modes.
- **MQTT Discovery**: Automatically creates a "Water Heater" device in Home Assistant.
- **Diagnostic Sensors**: Monitor errors and system status.

## Installation

1. Prerequisite: Install and start Mosquitto MQTT broker within Home Assistant.
2. Click the **Add Repository** button above, or manually add `https://github.com/waterheater-dev/ha-thermowatt-heater` to your Home Assistant Add-on Store.
3. Install the **Thermowatt Smart Boiler** add-on.
4. Configure your Thermowatt account credentials in the **Configuration** tab.
5. Start the add-on.

## Configuration

```yaml
email: "your-email@example.com"
password: "your-password"
```

## Dashboard

Once the add-on is running, a new entity will appear under your MQTT integration. We recommend using the Thermostat Card for the best experience.

## Troubleshooting

Since 1.3.4, Python bridge logs use UTC ISO 8601 timestamps with milliseconds
and a severity level. Match these to Home Assistant history by converting UTC
to your local timezone (Sydney daylight time is UTC+11).

Each HTTP attempt logs the method, endpoint, HTTP status and elapsed seconds.
Device requests include the boiler name and serial. HTTP 200 means a response
arrived; a following `[STATUS] ... failed` still means its contents were rejected.
Request/response bodies, credentials, tokens and raw request exceptions are not logged.

For example, a failed status poll followed by recovery looks like:

```code
2026-11-15T00:52:08.936Z INFO [HTTP] Extension (example-serial) GET /status; status=200 duration=0.825s reason=response received
2026-11-15T00:52:08.937Z WARNING [AVAILABILITY] Extension (example-serial): data unavailable
2026-11-15T00:52:08.937Z WARNING [STATUS] Extension (example-serial): failed: unsuccessful or malformed cloud response / invalid value
2026-11-15T00:57:11.419Z INFO [HTTP] Extension (example-serial) GET /status; status=200 duration=0.740s reason=response received
2026-11-15T00:57:11.420Z INFO [RECOVERY] Extension (example-serial): fresh status received; data unavailable for 302.5s
```

Availability/recovery messages appear once per transition, including startup and
MQTT reconnection. The duration starts when this process publishes the first
offline state, uses a monotonic clock, and does not survive a bridge restart.
It measures unavailable bridge data, not a confirmed physical heater outage.
Container service-manager output may keep its own format.

## Known to work on:

- **Home Assistant:**
  - _Installation method:_ Home Assistant OS
  - _Core:_ 2025.12.5
  - _Supervisor:_ 2026.01.1
  - _Operating System:_ 16.3
  - _Frontend:_ 20251203.3
- **Mosquitto MQTT Version:** 6.5.2
- **MyThermowatt App Version:** 3.14

Tip: Help others by adding your version here, if it works.

---

_Disclaimer: This project is not affiliated with or endorsed by Thermowatt or Ariston._

---

### Support my work

If this add-on saved you some frustration or made your home a bit smarter or helped someone avoid a cold shower, feel free to [buy me a beer on Ko-fi!](https://ko-fi.com/thermohacker)

[![support](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/thermohacker)


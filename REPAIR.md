# Thermowatt bridge reliability repair

## Version 1.3.3: visible cooldown status

The **Thermowatt Bridge** MQTT device exposes Bridge State, Bridge Heartbeat,
Cooldown Reason and Next Cloud Retry diagnostic sensors. Heartbeat updates
every minute, including during a saved cooldown; the app log reports the
remaining pause and UTC retry time immediately after MQTT connection and every
five minutes. HA displays the timestamp sensor in your local timezone. Next
Cloud Retry is unavailable when there is no scheduled cooldown.

These updates make no cloud requests. A running MQTT connection does not imply
that the heaters can currently be reached. The 24-hour daily-limit pause is a
conservative bridge policy, not a confirmed manufacturer lockout/reset time.
Upgrading preserves the previously saved deadline rather than starting a new
24-hour wait. Existing version 1.3.2 pauses display `saved_cloud_cooldown` because
that version did not save a reason.

Based on this fork's main branch (43b7ed3). Existing water-heater unique IDs,
discovery topics and diagnostic entities are preserved.

## Changes

- Configurable `poll_interval`, default 300 seconds per heater (60–3600 allowed).
  Two heaters now use about 576 scheduled status calls per day, plus startup,
  token refresh and command follow-up calls. The vendor quota is unknown.
- HTTP 200 responses containing `success: false` are failures, not status updates.
  A daily-limit response pauses all cloud activity for 24 hours; the actual vendor
  reset time is unknown. The pause is saved in the app's data and survives restart.
  HTTP 429 uses increasing waits and respects a longer Retry-After value.
- Error payloads never replace retained successful status. Both heaters become
  unavailable during an account-level pause. Individual failed polls mark that
  heater unavailable until its next valid response. MQTT last-will marks the
  bridge unavailable if it disconnects. Reconnect requires fresh status.
- Missing/non-numeric temperature readings are rejected, rather than shown as 0.
  Successful status includes `last_successful_update` in UTC.
- Commands are queued, with cloud traffic handled sequentially on one thread.
  Retained commands are ignored. Failed commands do not alter the remembered
  setpoint or invent a new state. Accepted commands schedule one real status
  fetch after 20 seconds. Only cloud-reported state is displayed; it may take
  a subsequent regular poll for a slow cloud update to appear.
- Network calls have timeouts. Only HTTP 401 prompts token refresh; unrelated
  errors do not cause repeated login attempts.
- The existing unverified anti-legionella indicator now reports unknown rather
  than inferring an active cycle from Cmd=0. No sanitation settings are changed.

## Install and verify

1. Before switching versions, record the installed version and app configuration,
   and take a backup of the installed Thermowatt app including its data. Do not
   uninstall the existing app before securing the backup.
2. Merge the repair PR into **your fork's main branch**. HA repository installs
   normally read main, so opening a PR alone will not make the repair installable.
3. In Settings → Apps → app store/repositories, add your fork's URL if needed:
   `https://github.com/echoztrip/ha-thermowatt-heater`.
   Identify the fork's version 1.3.2 explicitly; it has the same display name as
   upstream. If HA shows it as a separate app, copy the credentials through the
   configuration UI. Do not run two bridges against the same heaters.
4. Stop the old Thermowatt app, then start version 1.3.2 with `poll_interval: 300`.
   Keep the old installation stopped for rollback if HA installs the fork separately.
5. If the vendor daily quota is already exhausted, readings will remain
   unavailable during the saved 24-hour pause. Restarting does not clear it.
6. Once successful readings resume, compare `current_temperature` and `temperature`
   with the vendor app. They map to T_Avg and T_SetPoint respectively. Neither
   should be substituted for the other.
7. Send one deliberate command during normal use. Confirm a subsequent real
   status update and check the matching Shelly channel for actual heating power.
   Cloud acceptance alone does not prove that the element is running.
8. Check both heaters keep updating over a full day without request-limit errors.
   Then repair automations to handle unavailable temperature readings explicitly;
   existing float(99)/float(0) fallbacks are still unsuitable readiness checks.

Rollback: stop the fork app and start the preserved original, or restore the
Thermowatt app and its data from the backup. Do not leave both bridges running.

## Local validation

Install `requests`, `paho-mqtt`, `PyYAML`, and `jinja2`, then run:

```sh
python -m unittest discover -s tests -v
python -m py_compile thermowatt-heater/thermowatt_bridge.py
```

Tests use mocked MQTT and HTTP; they never connect to a boiler or use credentials.
CI builds the amd64 container and checks dependencies and the startup interpreter.
This repair has not been verified against live heaters.

import sys, json, time, uuid, os, requests, urllib3, datetime
import math, queue
from email.utils import parsedate_to_datetime
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- CONFIGURATION ---
EMAIL = sys.argv[1] if len(sys.argv) > 1 else None
PASSWORD = sys.argv[2] if len(sys.argv) > 2 else None
CONFIG_FILE = "/data/thermowatt_config.json" if os.path.exists("/data") else "thermowatt_config.json"
MQTT_HOST = os.getenv("MQTT_HOST", "core-mosquitto")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
MQTT_USER = os.getenv("MQTT_USER")
MQTT_PASS = os.getenv("MQTT_PASSWORD")

class MyThermowattBridge:
    API_KEY = "YVjArWssxKH631jv1dnnWOTr6gijsSAGz7rQJ4hJoUNRffxYvbQaMbePBEZalena"
    BASE_URL = "https://myapp-connectivity.com/api/v1"
    POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "300"))
    STATUS_LOG_INTERVAL = 300   # 5 minutes

    def __init__(self):
        self.config = self._load_config()
        self.session = requests.Session()
        self.mqtt_client = mqtt.Client(CallbackAPIVersion.VERSION2)
        if MQTT_USER:
            self.mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)

        # Polling state
        self.poll_count = 0
        self.success_count = 0
        self.error_count = 0
        self.last_status_log_time = time.time()
        if not 60 <= self.POLL_INTERVAL <= 3600:
            raise ValueError("POLL_INTERVAL must be from 60 to 3600 seconds")
        self.commands = queue.Queue(maxsize=20)
        self.pending_commands = {}
        self.retry_attempt = 0
        self.mqtt_client.will_set("thermowatt/bridge/availability", "offline", retain=True)
        self.mqtt_client.on_connect = self.on_connect

    def _load_config(self):
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                config = json.load(f)
                if 'devices' not in config:
                    config['devices'] = {}
                return config
        return {
            "device_uuid": str(uuid.uuid4()),
            "access_token": None,
            "refresh_token": None,
            "devices": {}
        }

    def _save_config(self):
        temporary = CONFIG_FILE + ".tmp"
        with open(temporary, 'w') as f:
            json.dump(self.config, f)
        os.replace(temporary, CONFIG_FILE)

    def _reset_headers(self):
        auth_header = self.session.headers.get("Authorization")
        self.session.headers.clear()
        if auth_header:
            self.session.headers["Authorization"] = auth_header
        self.session.headers.update({
            "app": "MyThermowatt",
            "platform": "iOS",
            "version": "3.14",
            "lang": "en"
        })

    def _update_auth(self, access, refresh):
        self.config.update({"access_token": access, "refresh_token": refresh})
        self.session.headers["Authorization"] = f"Bearer {access}"
        self._save_config()

    def login(self):
        if time.time() < self.config.get("blocked_until", 0):
            raise RuntimeError("Cloud requests paused after rate limiting")
        self._reset_headers()
        self.session.headers["x-api-key"] = self.API_KEY
        payload = {
            "username": EMAIL,
            "password": PASSWORD,
            "device_id": self.config["device_uuid"]
        }
        r = self.session.post(f"{self.BASE_URL}/login", json=payload, verify=False, timeout=(10, 30))
        res = self._validate(r)['result']
        self._update_auth(res['accessToken'], res['refreshToken'])

    def refresh_session(self):
        if time.time() < self.config.get("blocked_until", 0):
            raise RuntimeError("Cloud requests paused after rate limiting")
        self._reset_headers()
        self.session.headers["x-api-key"] = self.API_KEY
        payload = {"username": EMAIL, "refreshToken": self.config["refresh_token"]}
        r = self.session.post(f"{self.BASE_URL}/refresh", json=payload, verify=False, timeout=(10, 30))
        if r.status_code == 200:
            res = self._validate(r)['result']
            self._update_auth(res['accessToken'], res['refreshToken'])
            return True
        self._validate(r)
        return False

    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            return
        client.publish("thermowatt/bridge/availability", "online", retain=True)
        for serial, device in self.config.get("devices", {}).items():
            self.publish_discovery(serial, device["name"])
            self.availability(serial, False)
            client.subscribe(f"P/{serial}/CMD/#")

    def availability(self, serial, available):
        self.mqtt_client.publish(f"P/{serial}/availability",
                                 "online" if available else "offline", retain=True)

    def _pause(self, seconds):
        self.config["blocked_until"] = max(self.config.get("blocked_until", 0),
                                            time.time() + seconds)
        self._save_config()
        for serial in self.config.get("devices", {}):
            self.availability(serial, False)

    def _validate(self, response):
        try:
            body = response.json()
        except ValueError:
            body = None
        error = str(body.get("error", "")) if isinstance(body, dict) else ""
        daily = "daily" in error.lower() and "limit" in error.lower()
        limited = response.status_code == 429 or "limit exceeded" in error.lower()
        if limited:
            self.retry_attempt += 1
            delay = 86400 if daily else min(3600, self.POLL_INTERVAL * 2 ** min(self.retry_attempt, 6))
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = max(delay, float(retry_after))
                except ValueError:
                    try:
                        delay = max(delay, parsedate_to_datetime(retry_after).timestamp() - time.time())
                    except (ValueError, TypeError, OverflowError):
                        pass
            self._pause(delay)
            raise RuntimeError(f"Cloud request limit reached; paused for {delay:.0f}s")
        response.raise_for_status()
        if not isinstance(body, dict) or body.get("success") is False:
            raise ValueError("Cloud returned an unsuccessful or malformed response")
        return body

    def request(self, method, endpoint, serial=None, **kwargs):
        # All requests run on the main thread; MQTT callbacks only enqueue commands.
        if time.time() < self.config.get("blocked_until", 0):
            raise RuntimeError("Cloud requests paused after rate limiting")
        self._reset_headers()
        if serial:
            self.session.headers["seriale"] = serial
        kwargs.setdefault("timeout", (10, 30))
        response = self.session.request(method, f"{self.BASE_URL}{endpoint}",
                                        verify=False, **kwargs)
        if response.status_code == 401 and self.refresh_session():
            self._reset_headers()
            if serial:
                self.session.headers["seriale"] = serial
            response = self.session.request(method, f"{self.BASE_URL}{endpoint}",
                                            verify=False, **kwargs)
        self._validate(response)
        return response

    # -------------------------------------------------------------------------
    # MQTT Discovery
    # -------------------------------------------------------------------------

    def _pub(self, topic, payload):
        """Helper to publish a retained JSON discovery message."""
        if "state_topic" in payload or "current_temperature_topic" in payload:
            serial = payload["device"]["identifiers"][0].removeprefix("tw_")
            payload["availability"] = [
                {"topic": "thermowatt/bridge/availability"},
                {"topic": f"P/{serial}/availability"},
            ]
            payload["availability_mode"] = "all"
            payload["payload_available"] = "online"
            payload["payload_not_available"] = "offline"
            if "/sensor/" in topic or "/binary_sensor/" in topic:
                payload["expire_after"] = self.POLL_INTERVAL * 3
        self.mqtt_client.publish(topic, json.dumps(payload), retain=True)

    def _device_block(self, serial, name):
        return {
            "identifiers": [f"tw_{serial}"],
            "manufacturer": "Thermowatt",
            "name": name
        }

    def _pub_sensor(self, serial, name, sensor_id, sensor_name, state_template,
                    device_class=None, unit=None, icon=None, entity_category=None):
        cfg = {
            "unique_id": f"thermowatt_{serial}_{sensor_id}",
            "name": f"{name} {sensor_name}",
            "state_topic": f"P/{serial}/STATUS",
            "value_template": state_template,
            "device": self._device_block(serial, name)
        }
        if device_class:        cfg["device_class"] = device_class
        if unit:                cfg["unit_of_measurement"] = unit
        if icon:                cfg["icon"] = icon
        if entity_category:     cfg["entity_category"] = entity_category
        self._pub(f"homeassistant/sensor/{serial}_{sensor_id}/config", cfg)

    def _pub_binary(self, serial, name, sensor_id, sensor_name, state_template,
                    device_class=None, icon=None, entity_category=None):
        cfg = {
            "unique_id": f"thermowatt_{serial}_{sensor_id}",
            "name": f"{name} {sensor_name}",
            "state_topic": f"P/{serial}/STATUS",
            "value_template": state_template,
            "payload_on": "True",
            "payload_off": "False",
            "device": self._device_block(serial, name)
        }
        if device_class:    cfg["device_class"] = device_class
        if icon:            cfg["icon"] = icon
        if entity_category: cfg["entity_category"] = entity_category
        self._pub(f"homeassistant/binary_sensor/{serial}_{sensor_id}/config", cfg)

    def publish_discovery(self, serial, name):
        # ── Water Heater entity ───────────────────────────────────────────────
        self._pub(f"homeassistant/water_heater/{serial}/config", {
            "unique_id": f"thermowatt_{serial}_v314",
            "name": f"Boiler {name}",
            "temp_unit": "C",
            "min_temp": 20,
            "max_temp": 75,
            "optimistic": False,
            "current_temperature_topic": f"P/{serial}/STATUS",
            "current_temperature_template": "{{ value_json.result.T_Avg | float }}",
            "temperature_state_topic": f"P/{serial}/STATUS",
            "temperature_state_template": "{{ value_json.result.T_SetPoint | float }}",
            "temperature_command_topic": f"P/{serial}/CMD/TEMP",
            "mode_state_topic": f"P/{serial}/STATUS",
            "mode_state_template": (
                "{% set cmd = value_json.result.Cmd | default(0) | int %}"
                "{% if cmd == 9 %}Manual"
                "{% elif cmd == 3 %}Eco"
                "{% elif cmd == 17 %}Auto"
                "{% elif cmd == 65 %}Holiday"
                "{% elif cmd == 16 %}Off"
                # TODO: Add Anti-Legionella cmd value once confirmed from device observation.
                # When a legionella cycle is triggered via the app, watch the raw Cmd value
                # in the MQTT debug output and add it here, e.g.:
                # "{% elif cmd == 33 %}Anti-Legionella"
                "{% else %}Off{% endif %}"
            ),
            "mode_command_topic": f"P/{serial}/CMD/MODE",
            "modes": ["Off", "Eco", "Manual", "Auto", "Holiday"],
            "json_attributes_topic": f"P/{serial}/STATUS",
            "json_attributes_template": "{{ value_json.result | tojson }}",
            "device": self._device_block(serial, name)
        })

        # ── Heating active (uses injected heating field from poll_status) ─────
        self._pub_binary(
            serial, name,
            "heating", "Heating",
            "{{ value_json.result.heating }}",
            device_class="heat",
            icon="mdi:fire"
        )

        # ── Anti-Legionella cycle active ──────────────────────────────────────
        # TODO: Replace 0 with the actual Cmd value once observed from a live cycle.
        # Trigger a legionella cycle from the app and watch for an unknown Cmd value
        # in the add-on logs (the poll_status debug logging below will print it).
        self._pub_binary(
            serial, name,
            "legionella", "Anti-Legionella Active",
            "{{ none }}",
            icon="mdi:bacteria-outline"
        )

        # ── Temperature sensors ───────────────────────────────────────────────
        self._pub_sensor(
            serial, name,
            "tamb", "Ambient Temperature",
            "{{ value_json.result.TAmb | default(0) | float }}",
            device_class="temperature", unit="°C"
        )

        # ── Timing sensors ────────────────────────────────────────────────────
        self._pub_sensor(
            serial, name,
            "time_remaining", "Time Remaining",
            "{{ value_json.result.TimeRemaining | default(0) | int }}",
            unit="min", icon="mdi:timer-outline"
        )

        # ── Diagnostic sensors ────────────────────────────────────────────────
        self._pub_sensor(
            serial, name,
            "rssi", "Wi-Fi Signal",
            "{{ value_json.result.Rssi | default(0) | int }}",
            device_class="signal_strength", unit="dBm",
            entity_category="diagnostic"
        )

        self._pub_sensor(
            serial, name,
            "fw_tmec", "Thermostat Firmware",
            "{{ value_json.result.Tmec_SW_Version | default('unknown') }}",
            icon="mdi:chip",
            entity_category="diagnostic"
        )

        self._pub_sensor(
            serial, name,
            "fw_wifi", "Wi-Fi Module Firmware",
            "{{ value_json.result.Wifi_SW_Version | default('unknown') }}",
            icon="mdi:wifi-cog",
            entity_category="diagnostic"
        )

        self._pub_sensor(
            serial, name,
            "volatile_errors", "Volatile Errors",
            "{{ value_json.result.VolatileErrors | default(0) | int }}",
            icon="mdi:alert-circle-outline",
            entity_category="diagnostic"
        )

        self._pub_sensor(
            serial, name,
            "non_volatile_errors", "Non-Volatile Errors",
            "{{ value_json.result.NonVolatileErrors | default(0) | int }}",
            icon="mdi:alert-circle",
            entity_category="diagnostic"
        )

        # ── Binary diagnostic sensors ─────────────────────────────────────────
        self._pub_binary(
            serial, name,
            "drm", "Demand Response Active",
            "{{ (value_json.result.Drm_status | default(0) | int) != 0 }}",
            icon="mdi:transmission-tower"
        )

        self._pub_binary(
            serial, name,
            "ota", "Firmware Update Available",
            "{{ (value_json.result.Ota_status | default(0) | int) != 0 }}",
            device_class="update",
            entity_category="diagnostic"
        )

        # ── Energy sensors (stubbed — pending /stats endpoint investigation) ──
        # TODO: The /stats endpoint returns {"success":false,"error":"Wrong mode"}
        # regardless of query parameters tried (mode, period, type — both string
        # and numeric values, GET only — POST returns 405). Italian-language
        # parameter names and date-range variants were also tried without success.
        # These sensors are pre-registered so they appear in HA once the endpoint
        # is understood. They will show as unavailable until poll_stats() is
        # implemented. See: https://github.com/echoztrip/ha-thermowatt-heater/issues
        self._pub_sensor(
            serial, name,
            "energy_today", "Energy Today",
            "{{ value_json.energy_today | default(0) | float }}",
            device_class="energy", unit="kWh",
            icon="mdi:lightning-bolt",
            # Uses a separate topic fed by poll_stats() rather than the STATUS topic
        )
        # Override state_topic for energy sensors to use a dedicated topic
        for sensor_id, sensor_name in [
            ("energy_today",   "Energy Today"),
            ("energy_month",   "Energy This Month"),
            ("energy_total",   "Energy Total"),
        ]:
            cfg = {
                "unique_id": f"thermowatt_{serial}_{sensor_id}",
                "name": f"{name} {sensor_name}",
                "state_topic": f"P/{serial}/ENERGY",
                "value_template": f"{{{{ value_json.{sensor_id} | default(0) | float }}}}",
                "device_class": "energy",
                "unit_of_measurement": "kWh",
                "state_class": "total_increasing",
                "icon": "mdi:lightning-bolt",
                "device": self._device_block(serial, name)
            }
            self._pub(f"homeassistant/sensor/{serial}_{sensor_id}/config", cfg)

    # -------------------------------------------------------------------------
    # Commands (HA → API)
    # -------------------------------------------------------------------------

    def on_mqtt_message(self, client, userdata, msg):
        # Never replay retained commands after a restart/reconnect.
        if msg.retain:
            return
        try:
            parts = msg.topic.split('/')
            if len(parts) != 4 or parts[0] != "P" or parts[2] != "CMD":
                return
            serial, command = parts[1], parts[3]
            if serial not in self.config.get("devices", {}) or command not in {"TEMP", "MODE"}:
                return
            if time.time() < self.config.get("blocked_until", 0):
                print("[CMD] Rejected while cloud requests are paused")
                return
            self.commands.put_nowait((serial, command, msg.payload.decode()))
        except (UnicodeDecodeError, queue.Full):
            print("[CMD] Invalid payload or command queue full")

    def process_command(self, serial, command, payload):
        device = self.config["devices"][serial]
        try:
            if command == "TEMP":
                value = float(payload)
                if not math.isfinite(value) or not value.is_integer() or not 20 <= value <= 75:
                    raise ValueError("Temperature must be an integer from 20 to 75")
                self.request("POST", "/manual", serial=serial, json={"T_SetPoint": int(value)})
                device["last_setpoint"] = int(value)
            elif payload == "Manual":
                self.request("POST", "/manual", serial=serial,
                             json={"T_SetPoint": device.get("last_setpoint", 60)})
            elif payload in {"Eco", "Auto", "Off"}:
                self.request("POST", "/" + payload.lower(), serial=serial,
                             headers={"Content-Type": "text/plain"}, data="")
            elif payload == "Holiday":
                end = (datetime.datetime.now() + datetime.timedelta(days=30)).strftime("%Y-%m-%d")
                self.request("POST", "/holiday", serial=serial, json={"end_date": end})
            else:
                raise ValueError("Unsupported mode")
            self._save_config()
            # Allow the cloud time to reflect the command, without blocking MQTT.
            self.pending_commands[serial] = time.time() + 20
            print(f"[CMD] Accepted for {serial}; waiting for a real status update")
        except Exception as exc:
            self.availability(serial, False)
            print(f"[CMD] Failed for {serial}: {exc}")

    def poll_status(self, serial):
        try:
            status = self.request("GET", "/status", serial=serial).json()
            result = status.get("result")
            if not isinstance(result, dict):
                raise ValueError("Missing status result")
            # Do not present absent, non-numeric, or non-finite temperatures as zero.
            for key in ("T_Avg", "T_SetPoint", "Cmd", "WaterHeaterSts"):
                if isinstance(result.get(key), bool) or not math.isfinite(float(result[key])):
                    raise ValueError(f"Invalid {key}")
            result["heating"] = bool(int(result["WaterHeaterSts"]) & 1)
            result["last_successful_update"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            self.config["devices"][serial]["last_setpoint"] = int(float(result["T_SetPoint"]))
            self._save_config()
            self.mqtt_client.publish(f"P/{serial}/STATUS", json.dumps(status), retain=True)
            self.availability(serial, True)
            return True
        except Exception as exc:
            self.availability(serial, False)
            print(f"[STATUS] Failed for {serial}: {exc}")
            return False

    # -------------------------------------------------------------------------
    # Energy/stats polling (stubbed pending endpoint investigation)
    # -------------------------------------------------------------------------

    def poll_stats(self, serial):
        """
        Attempt to retrieve energy/consumption statistics from the API.

        STATUS: Under investigation. The /stats endpoint returns HTTP 200 but
        responds with {"success":false,"error":"Wrong mode"} regardless of the
        query parameters provided. The following approaches have been tried
        without success:

        GET /stats with params: mode, period, type — string values
            (daily/weekly/monthly/yearly/day/week/month/year) and numeric
            values (0–9), Italian-language values (giornaliero/mensile/annuale),
            and date-range combinations (from/to, start/end, date_from/date_to).

        POST /stats — returns HTTP 405 (GET only).

        Path variants with serial in URL rather than header — all 404.

        Italian endpoint names (/statistiche, /consumo, /energia) — all 404.

        Next steps:
        - Intercept app traffic via mitmproxy during energy screen navigation
          to observe the exact request the app makes.
        - Investigate whether "Wrong mode" refers to device operating state
          rather than a missing parameter (e.g. device must not be in Off mode).
        - Check whether a different API version prefix (v2, v3) is used for
          energy endpoints.

        Once the correct call is known, publish results to P/{serial}/ENERGY
        as JSON: {"energy_today": x, "energy_month": y, "energy_total": z}
        The HA sensor discovery configs are already registered in publish_discovery().
        """
        # TODO: implement once /stats endpoint behaviour is understood
        pass

    # -------------------------------------------------------------------------
    # Diagnostics
    # -------------------------------------------------------------------------

    def log_status_summary(self):
        elapsed = time.time() - self.last_status_log_time
        if elapsed >= self.STATUS_LOG_INTERVAL:
            print(f"[STATUS] Polled {self.poll_count} times, "
                  f"got {self.success_count} x 200, {self.error_count} errors")
            self.poll_count = 0
            self.success_count = 0
            self.error_count = 0
            self.last_status_log_time = time.time()

    # -------------------------------------------------------------------------
    # Boot and main loop
    # -------------------------------------------------------------------------

    def run(self):
        if not EMAIL or not PASSWORD:
            raise ValueError("Missing email/password in app configuration")
        self.mqtt_client.on_message = self.on_mqtt_message
        self.mqtt_client.connect(MQTT_HOST, MQTT_PORT, 60)
        self.mqtt_client.loop_start()
        booted = False
        next_poll = 0
        try:
            while True:
                if time.time() < self.config.get("blocked_until", 0):
                    # Drop commands rather than unexpectedly executing them hours later.
                    while not self.commands.empty():
                        self.commands.get_nowait()
                    self.pending_commands.clear()
                    time.sleep(1)
                    continue
                try:
                    if not booted:
                        self.login()
                        devices = self.request("GET", "/user-info").json()["result"]["termostati"]
                        if not devices:
                            raise ValueError("No thermostats returned")
                        active = {}
                        for item in devices:
                            serial = str(item["seriale"])
                            active[serial] = self.config["devices"].get(serial, {})
                            active[serial]["name"] = item.get("nome", "Boiler")
                        self.config["devices"] = active
                        self._save_config()
                        for serial, device in active.items():
                            self.publish_discovery(serial, device["name"])
                            self.availability(serial, False)
                            self.mqtt_client.subscribe(f"P/{serial}/CMD/#")
                        booted = True
                    # Drain a bounded snapshot; new messages wait until the next iteration.
                    for _ in range(self.commands.qsize()):
                        self.process_command(*self.commands.get_nowait())
                    due = time.time() >= next_poll
                    targets = list(self.config["devices"]) if due else [
                        serial for serial, when in self.pending_commands.items() if time.time() >= when
                    ]
                    if targets:
                        all_ok = True
                        for serial in targets:
                            if time.time() < self.config.get("blocked_until", 0):
                                break
                            self.poll_count += 1
                            ok = self.poll_status(serial)
                            self.success_count += int(ok)
                            self.error_count += int(not ok)
                            all_ok = all_ok and ok
                            self.pending_commands.pop(serial, None)
                        if all_ok:
                            self.retry_attempt = 0
                        if due:
                            next_poll = time.time() + self.POLL_INTERVAL
                    self.log_status_summary()
                except Exception as exc:
                    print(f"[BRIDGE] {exc}")
                    self._pause(self.POLL_INTERVAL)
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            self.mqtt_client.publish("thermowatt/bridge/availability", "offline", retain=True).wait_for_publish()
            self.mqtt_client.disconnect()
            self.mqtt_client.loop_stop()


if __name__ == "__main__":
    bridge = MyThermowattBridge()
    bridge.run()

import sys, json, time, uuid, os, requests, urllib3, datetime
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
    POLL_INTERVAL = 20          # seconds
    STATUS_LOG_INTERVAL = 300   # 5 minutes
    STATS_POLL_INTERVAL = 300   # poll energy stats every 5 minutes

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
        self.last_stats_poll_time = 0   # force immediate attempt on first run
        self.rate_limit_backoff = 0
        self.current_poll_interval = self.POLL_INTERVAL

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
        with open(CONFIG_FILE, 'w') as f:
            json.dump(self.config, f)

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
        self._reset_headers()
        self.session.headers["x-api-key"] = self.API_KEY
        payload = {
            "username": EMAIL,
            "password": PASSWORD,
            "device_id": self.config["device_uuid"]
        }
        r = self.session.post(f"{self.BASE_URL}/login", json=payload, verify=False)
        r.raise_for_status()
        res = r.json()['result']
        self._update_auth(res['accessToken'], res['refreshToken'])

    def refresh_session(self):
        self._reset_headers()
        self.session.headers["x-api-key"] = self.API_KEY
        payload = {"username": EMAIL, "refreshToken": self.config["refresh_token"]}
        r = self.session.post(f"{self.BASE_URL}/refresh", json=payload, verify=False)
        if r.status_code == 200:
            res = r.json()['result']
            self._update_auth(res['accessToken'], res['refreshToken'])
            return True
        return False

    def request(self, method, endpoint, serial=None, **kwargs):
        self._reset_headers()
        url = f"{self.BASE_URL}{endpoint}"
        if serial:
            self.session.headers["seriale"] = serial
        resp = self.session.request(method, url, verify=False, **kwargs)
        if resp.status_code == 401:
            if self.refresh_session():
                self._reset_headers()
                if serial:
                    self.session.headers["seriale"] = serial
                resp = self.session.request(method, url, verify=False, **kwargs)
        return resp

    # -------------------------------------------------------------------------
    # MQTT Discovery
    # -------------------------------------------------------------------------

    def _pub(self, topic, payload):
        """Helper to publish a retained JSON discovery message."""
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
            "optimistic": True,
            "current_temperature_topic": f"P/{serial}/STATUS",
            "current_temperature_template": "{{ value_json.result.T_Avg | default(0) | float }}",
            "temperature_state_topic": f"P/{serial}/STATUS",
            "temperature_state_template": "{{ value_json.result.T_SetPoint | default(0) | float }}",
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
            "{{ value_json.result.Cmd | default(0) | int == 0 }}",
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
        try:
            payload = msg.payload.decode()
            parts = msg.topic.split('/')
            if len(parts) < 2:
                return
            sn = parts[1]

            device_config = self.config.get('devices', {}).get(sn, {})
            if not device_config:
                print(f"⚠️  Unknown device serial: {sn}")
                return

            current_fav = device_config.get("last_setpoint", 60)

            if f"P/{sn}/CMD/TEMP" in msg.topic:
                temp = int(float(payload))
                print(f"[CMD] Setting Temperature to {temp}°C for {sn}...")
                self.request("POST", "/manual", serial=sn, json={"T_SetPoint": temp})
                device_config["last_setpoint"] = temp
                self.config['devices'][sn] = device_config
                self._inject_fake_status(sn, {"T_SetPoint": str(temp)})

            elif f"P/{sn}/CMD/MODE" in msg.topic:
                print(f"[CMD] Setting Mode to {payload} for {sn}...")

                if payload == "Manual":
                    self.request("POST", "/manual", serial=sn, json={"T_SetPoint": current_fav})
                    self._inject_fake_status(sn, {"Cmd": "9", "T_SetPoint": str(current_fav)})
                elif payload == "Eco":
                    self.request("POST", "/eco", serial=sn,
                                 headers={"Content-Type": "text/plain"}, data="")
                    self._inject_fake_status(sn, {"Cmd": "3"})
                elif payload == "Auto":
                    self.request("POST", "/auto", serial=sn,
                                 headers={"Content-Type": "text/plain"}, data="")
                    self._inject_fake_status(sn, {"Cmd": "17"})
                elif payload == "Holiday":
                    future_date = (datetime.datetime.now() +
                                   datetime.timedelta(days=30)).strftime("%Y-%m-%d")
                    print(f"[CMD] Setting Holiday Mode until {future_date} for {sn}...")
                    self.request("POST", "/holiday", serial=sn,
                                 json={"end_date": future_date})
                    self._inject_fake_status(sn, {"Cmd": "65"})
                elif payload == "Off":
                    print(f"[CMD] Turning Boiler OFF for {sn}...")
                    resp = self.request("POST", "/off", serial=sn,
                                        headers={"Content-Type": "text/plain"}, data="")
                    self._inject_fake_status(sn, {"Cmd": "16"})
                    if resp:
                        print(f"[SUCCESS] Boiler {sn} is now OFF: {resp.text}")

            self._save_config()
        except Exception as e:
            print(f"MQTT Cmd Error: {e}")

    # -------------------------------------------------------------------------
    # Status polling
    # -------------------------------------------------------------------------

    def _inject_fake_status(self, serial, overrides):
        try:
            r = self.request("GET", "/status", serial=serial)
            status = r.json()
            mqtt_status = status.get('result', {})
            for k, v in overrides.items():
                mqtt_status[k] = str(v)
            water_heater_sts = int(mqtt_status.get('WaterHeaterSts', 0))
            mqtt_status['heating'] = (water_heater_sts & 1) != 0
            self.mqtt_client.publish(f"P/{serial}/STATUS",
                                     json.dumps(status), retain=True)
        except Exception as e:
            print(f"⚠️  Status injection failed for {serial}: {e}")

    def poll_status(self, serial):
        try:
            r = self.request("GET", "/status", serial=serial)
            status_code = r.status_code

            if status_code == 200:
                status_data = r.json()
                if 'result' in status_data:
                    water_heater_sts = int(
                        status_data['result'].get('WaterHeaterSts', 0))
                    status_data['result']['heating'] = (water_heater_sts & 1) != 0

                    # Debug logging for unrecognised Cmd values.
                    # This is intentionally left in to help identify the
                    # Anti-Legionella cycle Cmd value. When a legionella cycle
                    # is running the Cmd value will appear here if it isn't
                    # already in the known set {3, 9, 16, 17, 65}.
                    cmd = int(status_data['result'].get('Cmd', 0))
                    if cmd not in {3, 9, 16, 17, 65}:
                        print(
                            f"[DEBUG] Unknown Cmd={cmd} for {serial} — "
                            f"WaterHeaterSts={water_heater_sts}, "
                            f"T_Avg={status_data['result'].get('T_Avg')}, "
                            f"TimeRemaining={status_data['result'].get('TimeRemaining')}"
                        )

                self.mqtt_client.publish(f"P/{serial}/STATUS",
                                         json.dumps(status_data), retain=True)
                return (True, status_code)
            else:
                return (False, status_code)
        except Exception as e:
            print(f"Poll error for {serial}: {e}")
            return (False, None)

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
        print("--- BOOT SEQUENCE START ---")

        if not EMAIL or not PASSWORD:
            print("FAILED: Step 1 - Missing EMAIL/PASSWORD in addon config.")
            sys.exit(1)
        print("OK: Step 1 - Credentials present.")

        try:
            self.mqtt_client.connect(MQTT_HOST, MQTT_PORT, 60)
            print("OK: Step 2 & 3 - Connected and authenticated with local MQTT.")
        except Exception as e:
            print(f"FAILED: Step 2/3 - MQTT Connection Error: {e}")
            sys.exit(1)

        try:
            self.login()
            print("OK: Step 4 - Logged in to Thermowatt backend.")
        except Exception as e:
            print(f"FAILED: Step 4 - Backend authentication failed: {e}")
            sys.exit(1)

        try:
            r = self.request("GET", "/user-info")
            devices = r.json().get('result', {}).get('termostati', [])
            if not devices:
                raise Exception("Zero devices returned")

            if 'devices' not in self.config:
                self.config['devices'] = {}

            print(f"OK: Step 5 - Found {len(devices)} thermostats.")

            for dev in devices:
                serial = dev['seriale']
                name = dev.get('nome', 'Boiler')

                if serial not in self.config['devices']:
                    self.config['devices'][serial] = {
                        "name": name, "last_setpoint": 60
                    }
                else:
                    self.config['devices'][serial]["name"] = name

                self.publish_discovery(serial, name)
                self.mqtt_client.subscribe(f"P/{serial}/CMD/#")
                print(f"🌉 Bridge active for: {name} ({serial})")

            self._save_config()

        except Exception as e:
            print(f"FAILED: Step 5 - Could not retrieve thermostat list: {e}")
            sys.exit(1)

        print("OK: Step 6 - Booted successfully.")

        self.mqtt_client.on_message = self.on_mqtt_message
        self.mqtt_client.loop_start()

        print(f"OK: Step 7 - Starting polling loop (interval: {self.POLL_INTERVAL}s).")

        while True:
            try:
                rate_limited = False

                for serial in self.config.get('devices', {}).keys():
                    self.poll_count += 1
                    success, status_code = self.poll_status(serial)

                    if success:
                        self.success_count += 1
                        if self.rate_limit_backoff > 0:
                            self.rate_limit_backoff = 0
                            self.current_poll_interval = self.POLL_INTERVAL
                    else:
                        self.error_count += 1
                        if status_code == 429:
                            rate_limited = True
                            self.rate_limit_backoff += 1
                            if self.rate_limit_backoff == 1:
                                self.current_poll_interval = 20
                            elif self.rate_limit_backoff == 2:
                                self.current_poll_interval = 40
                            else:
                                self.current_poll_interval = 60
                            print(f"[RATE LIMIT] 429 received, backing off to "
                                  f"{self.current_poll_interval}s interval")
                            break
                        elif status_code is not None:
                            print(f"[ERROR] Got status {status_code}, re-logging in...")
                            try:
                                self.login()
                            except Exception as e:
                                print(f"[ERROR] Re-login failed: {e}")

                # Poll energy stats on a slower cadence
                if time.time() - self.last_stats_poll_time >= self.STATS_POLL_INTERVAL:
                    for serial in self.config.get('devices', {}).keys():
                        self.poll_stats(serial)
                    self.last_stats_poll_time = time.time()

                self.log_status_summary()
                time.sleep(self.current_poll_interval)

            except KeyboardInterrupt:
                print("Stopping...")
                break
            except Exception as e:
                print(f"[ERROR] Polling loop error: {e}")
                try:
                    self.login()
                except Exception as e2:
                    print(f"[ERROR] Re-login failed: {e2}")
                time.sleep(self.current_poll_interval)

        self.mqtt_client.disconnect()


if __name__ == "__main__":
    bridge = MyThermowattBridge()
    bridge.run()

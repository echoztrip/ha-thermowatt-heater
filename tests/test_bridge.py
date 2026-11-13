import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import jinja2
import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('bridge', ROOT / 'thermowatt-heater/thermowatt_bridge.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


def response(body, code=200, headers=None):
    result = requests.Response()
    result.status_code = code
    result._content = json.dumps(body).encode()
    result.headers.update(headers or {})
    return result


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / 'config.json')
        self.addCleanup(patch.stopall)
        patch.object(bridge, 'CONFIG_FILE', self.path).start()
        patch.object(bridge.mqtt, 'Client', return_value=MagicMock()).start()
        self.b = bridge.MyThermowattBridge()
        self.b.config['devices'] = {'main': {'name': 'Main', 'last_setpoint': 60},
                                    'extension': {'name': 'Extension'}}
        self.b.session = MagicMock()
        self.b.session.headers = {}

    def publications(self, suffix):
        return [call for call in self.b.mqtt_client.publish.call_args_list
                if call.args[0].endswith(suffix)]

    def valid(self):
        return {'success': True, 'result': {'T_Avg': '43.3', 'T_SetPoint': '60',
                                            'Cmd': '9', 'WaterHeaterSts': '1'}}

    def test_daily_limit_in_http_200_preserves_status_and_blocks_all_requests(self):
        self.b.session.request.return_value = response({'success': False, 'error': 'Daily request limit exceeded'})
        self.assertFalse(self.b.poll_status('main'))
        self.assertFalse(self.publications('/STATUS'))
        self.assertGreater(self.b.config['blocked_until'], bridge.time.time() + 86390)
        self.assertFalse(self.b.poll_status('extension'))
        self.assertEqual(self.b.session.request.call_count, 1)
        offline = {c.args[0] for c in self.publications('/availability') if c.args[1] == 'offline'}
        self.assertEqual(offline, {'P/main/availability', 'P/extension/availability'})
        # Restarting preserves the pause; login also cannot bypass it.
        restarted = bridge.MyThermowattBridge()
        restarted.session = MagicMock()
        with self.assertRaises(RuntimeError):
            restarted.login()
        restarted.session.post.assert_not_called()

    def test_http_429_honours_retry_after(self):
        with self.assertRaises(RuntimeError):
            self.b._validate(response({}, 429, {'Retry-After': '7200'}))
        self.assertGreater(self.b.config['blocked_until'], bridge.time.time() + 7190)

    def test_http_429_date_retry_after(self):
        from email.utils import formatdate
        date = formatdate(bridge.time.time() + 8000, usegmt=True)
        with self.assertRaises(RuntimeError):
            self.b._validate(response({}, 429, {'Retry-After': date}))
        self.assertGreater(self.b.config['blocked_until'], bridge.time.time() + 7990)

    def test_valid_reading_publishes_real_temperatures_and_heating(self):
        self.b.session.request.return_value = response(self.valid())
        self.assertTrue(self.b.poll_status('main'))
        status = json.loads(self.publications('/STATUS')[0].args[1])['result']
        self.assertEqual(status['T_Avg'], '43.3')
        self.assertEqual(status['T_SetPoint'], '60')
        self.assertTrue(status['heating'])
        self.assertIn('last_successful_update', status)
        self.assertEqual(self.b.config['devices']['main']['last_setpoint'], 60)
        self.assertEqual(self.publications('/availability')[-1].args[1], 'online')

    def test_bad_status_never_publishes_zero_or_replaces_retained_reading(self):
        for body in ({'success': False}, {}, {'result': {}},
                     {'result': {'T_Avg': None}},
                     {'result': dict(self.valid()['result'], T_Avg='nan')},
                     {'result': dict(self.valid()['result'], T_SetPoint=True)}):
            with self.subTest(body=body):
                self.b.session.request.return_value = response(body)
                self.assertFalse(self.b.poll_status('main'))
                self.assertFalse(self.publications('/STATUS'))

    def test_timeout_marks_unavailable(self):
        self.b.session.request.side_effect = requests.Timeout()
        self.assertFalse(self.b.poll_status('main'))
        self.assertEqual(self.publications('/availability')[-1].args[1], 'offline')
        self.assertEqual(self.b.session.request.call_args.kwargs['timeout'], (10, 30))

    def test_rejected_command_never_fakes_success(self):
        self.b.session.request.return_value = response({'success': False, 'error': 'Wrong mode'})
        self.b.process_command('main', 'TEMP', '55')
        self.assertEqual(self.b.config['devices']['main']['last_setpoint'], 60)
        self.assertFalse(self.b.pending_commands)
        self.assertFalse(self.publications('/STATUS'))

    def test_accepted_command_waits_for_real_status(self):
        self.b.session.request.return_value = response({'success': True})
        self.b.process_command('main', 'TEMP', '55')
        self.assertEqual(self.b.config['devices']['main']['last_setpoint'], 55)
        self.assertIn('main', self.b.pending_commands)
        self.assertFalse(self.publications('/STATUS'))
        self.assertGreater(self.b.pending_commands['main'], bridge.time.time() + 19)
        self.assertEqual(self.b.session.request.call_count, 1)

    def test_invalid_commands_do_not_reach_cloud(self):
        for payload in ('nan', 'inf', '19', '76', '55.5', 'garbage'):
            self.b.process_command('main', 'TEMP', payload)
        self.b.process_command('main', 'MODE', 'unsupported')
        self.b.session.request.assert_not_called()

    def test_mqtt_callback_queues_without_cloud_io_and_rejects_retained(self):
        msg = SimpleNamespace(topic='P/main/CMD/TEMP', payload=b'55', retain=True)
        self.b.on_mqtt_message(None, None, msg)
        self.assertTrue(self.b.commands.empty())
        msg.retain = False
        self.b.on_mqtt_message(None, None, msg)
        self.assertEqual(self.b.commands.get_nowait(), ('main', 'TEMP', '55'))
        self.b.session.request.assert_not_called()

    def test_reconnect_resubscribes_and_requires_fresh_status(self):
        self.b.on_connect(self.b.mqtt_client, None, None, 0, None)
        self.assertEqual(self.b.mqtt_client.subscribe.call_count, 2)
        self.assertEqual(self.publications('/availability')[-1].args[1], 'offline')

    def test_discovery_keeps_identity_and_templates_compile(self):
        self.b.publish_discovery('main', 'Main')
        configs = {c.args[0]: json.loads(c.args[1]) for c in self.publications('/config')}
        heater = configs['homeassistant/water_heater/main/config']
        self.assertEqual(heater['unique_id'], 'thermowatt_main_v314')
        self.assertFalse(heater['optimistic'])
        self.assertEqual(heater['availability_mode'], 'all')
        env = jinja2.Environment()
        for config in configs.values():
            for key, value in config.items():
                if key.endswith('template'):
                    env.from_string(value)
        measured = env.from_string(heater['current_temperature_template'])
        target = env.from_string(heater['temperature_state_template'])
        self.assertEqual(measured.render(value_json=self.valid()), '43.3')
        self.assertEqual(target.render(value_json=self.valid()), '60.0')

    def test_config_exposes_poll_interval(self):
        config = yaml.safe_load((ROOT / 'thermowatt-heater/config.yaml').read_text())
        self.assertEqual(config['options']['poll_interval'], 300)
        self.assertEqual(config['schema']['poll_interval'], 'int(60,3600)')

    def test_main_loop_polls_both_heaters_without_extra_logins(self):
        self.b.login = MagicMock()
        self.b.session.request.side_effect = [
            response({'success': True, 'result': {'termostati': [
                {'seriale': 'main', 'nome': 'Main'}, {'seriale': 'extension', 'nome': 'Extension'}]}}),
            response(self.valid()), response(self.valid()),
        ]
        with patch.object(bridge, 'EMAIL', 'test'), patch.object(bridge, 'PASSWORD', 'test'), \
             patch.object(bridge.time, 'sleep', side_effect=KeyboardInterrupt):
            self.b.run()
        self.b.login.assert_called_once()
        self.assertEqual(self.b.session.request.call_count, 3)
        self.assertEqual(len(self.publications('/STATUS')), 2)

    def test_main_loop_quota_error_stops_before_second_heater(self):
        self.b.login = MagicMock()
        self.b.session.request.side_effect = [
            response({'success': True, 'result': {'termostati': [
                {'seriale': 'main'}, {'seriale': 'extension'}]}}),
            response({'success': False, 'error': 'Daily request limit exceeded'}),
        ]
        with patch.object(bridge, 'EMAIL', 'test'), patch.object(bridge, 'PASSWORD', 'test'), \
             patch.object(bridge.time, 'sleep', side_effect=KeyboardInterrupt):
            self.b.run()
        self.assertEqual(self.b.session.request.call_count, 2)
        self.assertFalse(self.publications('/STATUS'))


if __name__ == '__main__':
    unittest.main()

from compose_test_utils import load_compose
import errno
import contextlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'web'))
import proxy_environment as proxy
from manager import Manager, read_vars, update_vars


def response(data=None, code=0):
    return SimpleNamespace(returncode=code, stdout=json.dumps(data) if data is not None else '', stderr='')


class ProxyEnvironmentTests(unittest.TestCase):
    def fixture(self, args):
        if args[:2] == ['ps', '-a']:
            return SimpleNamespace(returncode=0, stdout=json.dumps({'ID': 'a'*12, 'Names': 'npm-prod',
                'Image': 'jc21/nginx-proxy-manager:latest', 'Ports': '0.0.0.0:80->80/tcp, 0.0.0.0:443->443/tcp'}))
        if args[0] == 'inspect':
            self.assertNotIn('.Config.Env', args[2])
            return response({'name': '/npm-prod', 'running': True, 'networks': {'bridge': {}, 'npm_default': {}},
                             'mounts': [{'Source': '/secret-path', 'Destination': '/data'}]})
        if args[:2] == ['network', 'inspect']:
            return response({'Name': args[-1], 'Driver': 'bridge', 'Internal': False})
        raise AssertionError(args)

    @patch.object(proxy, 'busy_web_ports', return_value=([], True))
    def test_detect_npm_network_and_ports_without_returning_mounts_or_secrets(self, ports):
        with patch.object(proxy, 'docker', side_effect=self.fixture):
            detected = proxy.detect_environment()
        self.assertTrue(detected['available']);self.assertTrue(detected['complete'])
        self.assertEqual(detected['busy_ports'], [80, 443])
        self.assertEqual(detected['proxies'][0]['networks'], ['npm_default'])
        self.assertEqual(detected['proxies'][0]['kind'], 'Nginx Proxy Manager')
        self.assertNotIn('/secret-path', json.dumps(detected))

    @patch.object(proxy, 'busy_web_ports', return_value=([], True))
    def test_empty_host_missing_docker_and_unavailable_are_distinct(self, ports):
        with patch.object(proxy, 'docker', return_value=response()):
            result = proxy.detect_environment()
        self.assertTrue(result['available']);self.assertEqual(result['proxies'], [])
        with patch.object(proxy, 'docker', side_effect=FileNotFoundError()):
            self.assertIn('尚未安装', proxy.detect_environment()['message'])
        with patch.object(proxy, 'docker', return_value=response(code=1)):
            result = proxy.detect_environment()
        self.assertFalse(result['available']);self.assertIn('无读取权限', result['message'])

    def test_missing_builtin_internal_and_unattachable_networks_rejected(self):
        for name in ('host', 'none', 'bridge', 'x; echo secret'):
            with self.subTest(name=name), patch.object(proxy, 'docker') as docker, self.assertRaises(ValueError):
                proxy.existing_network(name)
            docker.assert_not_called()
        for data, code in [(None, 1), ({'Driver': 'bridge', 'Internal': True}, 0),
                           ({'Driver': 'overlay', 'Attachable': False}, 0)]:
            with patch.object(proxy, 'docker', return_value=response(data, code)), self.assertRaises(ValueError):
                proxy.existing_network('npm_default')

    @patch.object(proxy, 'busy_web_ports', return_value=([443], True))
    def test_caddy_conflicts_and_own_running_caddy(self, ports):
        with patch.object(proxy, 'docker', return_value=response(code=1)), self.assertRaisesRegex(ValueError, '443'):
            proxy.ensure_caddy_available()
        info = {'running': True, 'mounts': [{'Source': '/opt/vault/caddy/Caddyfile', 'Destination': '/etc/caddy/Caddyfile'}]}
        with patch.object(proxy, 'docker', return_value=response(info)):
            with self.assertRaisesRegex(ValueError, '已有名为 caddy'):
                proxy.ensure_caddy_available()
            proxy.ensure_caddy_available('/opt/vault/caddy/Caddyfile')
        with patch.object(proxy, 'docker', side_effect=FileNotFoundError()), patch.object(proxy, 'busy_web_ports', return_value=([], True)):
            proxy.ensure_caddy_available()  # Config may be saved before installing Docker.

    @patch.object(proxy, 'busy_web_ports', return_value=([], True))
    def test_caddy_rejects_docker_published_ports_without_a_host_listener(self, ports):
        def inspect(args):
            if args[0] == 'inspect':return response(code=1)
            return SimpleNamespace(returncode=0, stdout=json.dumps({'Ports': '0.0.0.0:80->80/tcp'}))
        with patch.object(proxy, 'docker', side_effect=inspect), self.assertRaisesRegex(ValueError, '80'):
            proxy.ensure_caddy_available()

    def test_socket_check_detects_host_port_conflicts(self):
        error = OSError(errno.EADDRINUSE, 'occupied')
        with patch.object(proxy.socket, 'socket') as factory:
            factory.return_value.__enter__.return_value.bind.side_effect = error
            self.assertEqual(proxy.busy_web_ports(), ([80, 443], True))


class ProxyConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.root = Path(self.temp.name)
        self.manager = Manager(self.root/'state', self.root/'memory')
        self.data = dict(DOMAIN='https://vault.example.com', directory=str(self.root/'deployment'),
                         mode='docker_ext', network='npm_default', port=8080,
                         SIGNUPS_ALLOWED=False, INVITATIONS_ALLOWED=True, SENDS_ALLOWED=False)

    def tearDown(self):
        if self.manager.logfile:self.manager.logfile.close()
        self.temp.cleanup()

    def test_external_network_validation_happens_before_writing(self):
        with patch('manager.existing_network', side_effect=ValueError('网络不存在')), self.assertRaises(ValueError):
            self.manager.save('core', self.data)
        self.assertFalse((self.root/'deployment').exists());self.assertFalse(self.manager.memory.exists())
        with patch('manager.existing_network', return_value='npm_default'):
            self.manager.save('core', self.data)
        compose = load_compose(self.root/'deployment/vaultwarden/docker-compose.yml')
        self.assertEqual(list(compose['services']['vaultwarden']['networks']), ['npm_default'])
        self.assertNotIn('ports', compose['services']['vaultwarden'])
        self.assertEqual(read_vars(self.manager.memory)['PROXY_MODE'], 'docker_ext')

    def test_port_conflict_does_not_lock_in_a_caddy_draft(self):
        with patch('manager.ensure_caddy_available', side_effect=ValueError('443 已占用')), self.assertRaises(ValueError):
            self.manager.save('core', dict(self.data, mode='caddy'))
        self.assertFalse((self.root/'deployment').exists())

    def test_deploy_never_creates_a_missing_external_proxy_network(self):
        with patch('manager.existing_network', return_value='npm_default'):
            self.manager.save('core', self.data)
        update_vars(self.root/'deployment/vaultwarden/vaultwarden.env', {'ADMIN_TOKEN': '$argon2id$fixture'})
        calls = []
        with patch.object(self.manager, '_command', side_effect=lambda command, **kw: calls.append(command) or ''), \
             patch('manager.existing_network', side_effect=ValueError('网络已被删除')):
            self.manager._run('deploy', {}, contextlib.nullcontext())
        self.assertEqual(self.manager.task()['state'], 'failed')
        self.assertFalse(any(command[:3] == ['docker', 'network', 'create'] for command in calls))
        self.assertFalse(any('up' in command for command in calls))


if __name__ == '__main__':unittest.main()

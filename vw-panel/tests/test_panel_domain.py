import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from panel_domain import PanelNPM, configure, normalize, protect


class PanelDomainTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.manager = Mock()
        self.manager.state = Path(temp.name)
        self.manager.paths.return_value = (self.manager.state, self.manager.state)
        self.proxy = {'id': 'a'*12, 'name': 'my-npm', 'kind': 'Nginx Proxy Manager',
                      'running': True, 'https_published': True, 'networks': ['npm_default']}
        self.origin = 'https://panel.example.com'
        self.app = SimpleNamespace(state_dir=self.manager.state, config={'port': 8787, 'origins': []}, manager=self.manager)
        self.app.save_config = lambda config: setattr(self.app, 'config', config)
        self.app.domain = lambda origin: self.app.save_config(dict(self.app.config, origins=[origin]))

    def test_accepts_bare_domain_and_rejects_proxy_config_injection(self):
        self.assertEqual(normalize(' Panel.Example.com '), 'panel.example.com')
        self.assertEqual(normalize(self.origin), 'panel.example.com')
        for value in ('http://panel.example.com', 'panel.example.com:88', 'panel.example.com/path',
                      'panel.example.com\nfoo', '127.0.0.1', 'panel.example.com;'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize(value)

    def test_creates_separate_npm_site_with_panel_upstream(self):
        adapter = PanelNPM(self.manager, self.proxy, 'panel.example.com', '172.20.0.1', 8787,
                           '172.20.0.2', 'admin@example.com', 'secret')
        certificate = {'provider': 'letsencrypt', 'domain_names': ['panel.example.com'],
                       'expires_on': '2099-01-01T00:00:00Z', 'id': 9}
        with patch.object(adapter, '_npm_token', return_value='token'), patch.object(
            adapter, '_npm_request', side_effect=[[], [certificate], {'id': 10}]
        ) as request:
            adapter.configure()
        path, payload, token = request.call_args.args
        self.assertEqual(path, 'nginx/proxy-hosts')
        self.assertEqual((payload['forward_host'], payload['forward_port']), ('172.20.0.1', 8787))
        self.assertEqual(payload['domain_names'], ['panel.example.com'])
        self.assertEqual(payload['certificate_id'], 9)
        state = json.loads((adapter.folder / 'route-state.json').read_text())
        self.assertEqual(state['host_id'], 10)
        self.assertNotIn('secret', str(state))

    def test_failed_https_restores_prior_origins_and_preserves_direct_access(self):
        data = {'domain': 'panel.example.com', 'proxy': self.proxy['id'], 'email': '', 'password': ''}
        with patch('panel_domain.detect_environment', return_value={'proxies': [self.proxy]}), patch(
            'panel_domain.socket.getaddrinfo'
        ), patch('panel_domain.network_endpoint', return_value=('172.20.0.2','172.20.0.1','br-test')), patch(
            'panel_domain.PanelNPM'
        ), patch('panel_domain.verify_https', side_effect=ValueError('TLS')), patch('panel_domain.firewall') as firewall:
            with self.assertRaisesRegex(ValueError, 'TLS'):
                configure(self.app, data)
        firewall.assert_not_called()
        self.assertEqual(self.app.config['origins'], [])
        self.assertIsNone(self.app.domain_challenge)

    def test_successful_route_keeps_direct_access_until_domain_login(self):
        data = {'domain': 'panel.example.com', 'proxy': self.proxy['id'], 'email': '', 'password': ''}
        with patch('panel_domain.detect_environment', return_value={'proxies': [self.proxy]}), patch(
            'panel_domain.socket.getaddrinfo'
        ), patch('panel_domain.network_endpoint', return_value=('172.20.0.2', '172.20.0.1', 'br-test')), patch(
            'panel_domain.PanelNPM'
        ), patch('panel_domain.verify_https'), patch('panel_domain.firewall') as firewall:
            result = configure(self.app, data)
        firewall.assert_not_called()
        self.assertEqual(result['origin'], self.origin)
        self.assertFalse(self.app.config['panel_route']['protected'])

    def test_restriction_requires_domain_login_and_rolls_back_failed_second_probe(self):
        self.app.config['panel_route'] = {'origin': self.origin, 'port': 8787,
                                         'gateway': '172.20.0.1', 'interface': 'br-test'}
        with patch('panel_domain.firewall') as firewall, patch('panel_domain.verify_https') as verify, patch('panel_domain.panel_access.sync') as sync:
            with self.assertRaises(ValueError):
                protect(self.app, 'http://127.0.0.1:8787')
            firewall.assert_not_called()
            verify.side_effect = [None, ValueError('TLS')]
            with self.assertRaisesRegex(ValueError, 'TLS'):
                protect(self.app, self.origin)
            self.assertEqual(firewall.call_args_list, [call(8787, '172.20.0.1', 'br-test'), call()])
            self.assertFalse(self.app.config['panel_route'].get('protected'))
            self.assertEqual(sync.call_args_list, [call(self.app.state_dir, 8787, '172.20.0.1', 'br-test'), call(self.app.state_dir, 8787)])
            verify.side_effect = None
            protect(self.app, self.origin)
        self.assertTrue(self.app.config['panel_route']['protected'])

    def test_repair_recreated_container_updates_only_recorded_upstream_and_rolls_back(self):
        from panel_domain import repair
        self.app.config['panel_route'] = {'origin': self.origin, 'port': 8787, 'proxy': self.proxy['name'],
                                         'gateway': '172.20.0.1', 'interface': 'br-old', 'protected': True}
        root = self.app.state_dir / 'panel-npm'
        old_file = root / ('b'*12) / 'panel.example.com/route-state.json'
        old_file.parent.mkdir(parents=True)
        recorded = {'host_id': 7, 'domain': 'panel.example.com',
                    'forward_host': '172.20.0.1', 'forward_port': 8787}
        old_file.write_text(json.dumps(recorded))
        adapter = Mock(folder=root / self.proxy['id'] / 'panel.example.com')
        adapter._npm_token.return_value = 'fixture'
        adapter._npm_request.return_value = dict(recorded, domain_names=['panel.example.com'], forward_scheme='http')
        endpoint = ('172.21.0.2', '172.21.0.1', 'br-new')
        with patch('panel_domain.repair_target', return_value=(self.app.config['panel_route'], self.proxy, endpoint)), patch(
            'panel_domain.PanelNPM', return_value=adapter
        ), patch('panel_domain.firewall') as firewall, patch('panel_domain.panel_access.sync'), patch(
            'panel_domain.verify_https', side_effect=ValueError('TLS')
        ) as verify:
            with self.assertRaisesRegex(ValueError, 'TLS'):
                repair(self.app, {'email': '', 'password': ''})
            self.assertEqual(firewall.call_args_list, [call(8787, '172.21.0.1', 'br-new'), call(8787, '172.20.0.1', 'br-old')])
            self.assertEqual(adapter._npm_request.call_args.args[1],
                             {'forward_host': '172.20.0.1', 'forward_port': 8787})
            verify.side_effect = None
            repair(self.app, {'email': '', 'password': ''})
        self.assertEqual(self.app.config['panel_route']['gateway'], '172.21.0.1')
        self.assertTrue(self.app.config['panel_route']['protected'])
        self.assertEqual(adapter._npm_request.call_args.args,
                         ('nginx/proxy-hosts/7', {'forward_host': '172.21.0.1', 'forward_port': 8787}, 'fixture'))
        self.assertFalse((self.app.state_dir / 'panel-repair-probe.json').exists())

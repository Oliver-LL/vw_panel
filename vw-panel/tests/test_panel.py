import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('panel', ROOT / 'web/server.py')
panel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(panel)


def eventually(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.03)
    raise AssertionError('Timed out')


def credentials(directory):
    salt = os.urandom(16).hex()
    panel.atomic_json(directory / 'credentials.json', {'salt': salt, 'hash': panel.digest('TestPassword123!', salt)})


class TerminalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.script = Path(self.temp.name) / 'fixture.sh'
        self.script.write_text('''#!/bin/bash
printf '请选择操作 [1-18, q]: '
read -r action < /dev/tty
printf 'ACTION=%s\\n' "$action"
read -s -r -p 'Secret: ' secret < /dev/tty
printf '\\nSECRET_RECEIVED\\n'
read -r -p 'Finish: ' done < /dev/tty
exit 7
''')
        self.terminal = panel.Terminal(self.script)

    def tearDown(self):
        self.terminal.stop()
        eventually(lambda: self.terminal.fd is None)
        self.temp.cleanup()

    def test_controlling_tty_shortcut_password_refresh_and_exit(self):
        self.terminal.start('3')
        eventually(lambda: 'Secret:' in self.terminal.state()['output'])
        state = self.terminal.state()
        self.assertFalse(state['echo'])
        self.assertIn('ACTION=3', state['output'])
        self.terminal.send('MySecretPassword!\n')
        eventually(lambda: 'SECRET_RECEIVED' in self.terminal.state()['output'])
        self.assertNotIn('MySecretPassword!', self.terminal.state()['output'])
        self.assertTrue(self.terminal.state(0, '')['reset'])
        self.terminal.send('\n')
        eventually(lambda: self.terminal.state()['exit_code'] == 7)

    def test_all_18_shortcuts_reach_the_right_menu(self):
        for action in range(1, 19):
            with self.subTest(action=action):
                self.terminal.start(str(action))
                eventually(lambda: 'ACTION=%s' % action in self.terminal.state()['output'])
                self.terminal.stop()
                eventually(lambda: self.terminal.fd is None)

    def test_single_console_and_invalid_input(self):
        with self.assertRaises(ValueError):
            self.terminal.start('$(id)')
        self.terminal.start()
        with self.assertRaises(ValueError):
            self.terminal.start()
        with self.assertRaises(ValueError):
            self.terminal.send('a' * 4001)

    def test_cursor_truncation_and_reset(self):
        self.terminal.base = 50
        self.terminal.text = 'abc'
        generation = self.terminal.generation
        self.assertTrue(self.terminal.state(0, generation)['reset'])
        self.assertEqual(self.terminal.state(52, generation)['output'], 'c')

    def test_stop_kills_child_process(self):
        child_pid = Path(self.temp.name) / 'child.pid'
        self.script.write_text('sleep 120 &\necho $! > "' + str(child_pid) + '"\nwait\n')
        self.terminal.start()
        eventually(child_pid.exists)
        pid = int(child_pid.read_text())
        self.terminal.stop()
        eventually(lambda: self.terminal.fd is None)
        # A terminated child may briefly remain a zombie pending init's reap.
        def dead():
            path = Path('/proc/%s/stat' % pid)
            return not path.exists() or path.read_text().split()[2] == 'Z'
        eventually(dead)


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.state = Path(cls.temp.name)
        credentials(cls.state)
        cls.httpd = panel.ThreadingHTTPServer(('127.0.0.1', 0), panel.Handler)
        cls.port = cls.httpd.server_port
        panel.atomic_json(cls.state / 'panel.json', {'host':'0.0.0.0', 'port':cls.port, 'origins':[]})
        cls.httpd.app = panel.Application(cls.state)
        cls.url = 'http://127.0.0.1:%s' % cls.port
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def request(self, path, data=None, cookie=None, csrf=None, origin=None, host=None):
        headers = {'Origin':self.url if origin is None else origin}
        if cookie: headers['Cookie'] = cookie
        if csrf: headers['X-CSRF-Token'] = csrf
        if host: headers['Host'] = host
        if data is not None: headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(self.url + path, headers=headers, data=json.dumps(data).encode() if data is not None else None)
        try:
            response = urllib.request.urlopen(request)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read()
            value = json.loads(raw) if 'application/json' in response.headers.get('Content-Type','') else raw
            return response.status, value, response.headers

    def login(self):
        status, data, headers = self.request('/api/login', {'password':'TestPassword123!'})
        self.assertEqual(status, 200)
        return headers['Set-Cookie'].split(';')[0], data['csrf']

    def test_ip_login_without_domain(self):
        cookie, csrf = self.login()
        self.assertEqual(self.request('/api/session', cookie=cookie)[0], 200)
        self.assertEqual(self.request('/api/settings', cookie=cookie)[1]['port'], self.port)
        self.assertTrue(self.httpd.app.allowed_origin('http://203.0.113.12:%s' % self.port))
        self.assertTrue(self.httpd.app.allowed_origin('http://[::1]:%s' % self.port))
        self.assertFalse(self.httpd.app.allowed_origin('http://203.0.113.12:80'))

    def test_auth_csrf_and_logout(self):
        self.assertEqual(self.request('/api/terminal')[0], 401)
        cookie, csrf = self.login()
        self.assertEqual(self.request('/api/terminal/stop', {}, cookie=cookie)[0], 403)
        self.assertEqual(self.request('/api/terminal/stop', {}, cookie, csrf, 'https://evil.example')[0], 403)
        self.assertEqual(self.request('/api/logout', {}, cookie, csrf)[0], 200)
        self.assertEqual(self.request('/api/session', cookie=cookie)[0], 401)

    def test_domain_configuration_and_proxy_host(self):
        cookie, csrf = self.login()
        origin = 'https://panel.example.com'
        self.assertEqual(self.request('/', host='untrusted.example.com')[0], 403)
        self.assertEqual(self.request('/api/domain', {'origin':origin}, cookie, csrf)[0], 200)
        self.assertEqual(self.request('/', host='panel.example.com')[0], 200)
        status, data, headers = self.request('/api/login', {'password':'TestPassword123!'}, origin=origin, host='panel.example.com')
        self.assertEqual(status, 200)
        self.assertIn('; Secure', headers['Set-Cookie'])
        self.assertIn('; HttpOnly', headers['Set-Cookie'])
        self.assertIn(origin, json.loads((self.state/'panel.json').read_text())['origins'])
        self.assertEqual(self.request('/api/domain', {'origin':'https://other.example.com/path'}, cookie, csrf)[0], 409)
        self.assertEqual(self.request('/api/domain', {'origin':'http://insecure.example.com'}, cookie, csrf)[0], 409)

    def test_arbitrary_command_and_bad_route_rejected(self):
        cookie, csrf = self.login()
        self.assertEqual(self.request('/api/terminal/start', {'action':'$(id)'}, cookie, csrf)[0], 409)
        self.assertEqual(self.request('/api/terminal/start', {'action':{}}, cookie, csrf)[0], 409)
        self.assertEqual(self.request('/api/exec', {'command':'id'}, cookie, csrf)[0], 404)
        self.assertEqual(self.request('/../web/server.py', cookie=cookie)[0], 404)

    def test_static_security_headers(self):
        status, _, headers = self.request('/')
        self.assertEqual(status, 200)
        self.assertEqual(headers['X-Frame-Options'], 'DENY')
        self.assertIn("default-src 'self'", headers['Content-Security-Policy'])
        self.assertEqual(self.request('/app.js')[0], 200)
        self.assertEqual(self.request('/style.css')[0], 200)

    def test_login_rate_limit(self):
        self.httpd.app.attempts.clear()
        for _ in range(5):
            self.assertEqual(self.request('/api/login', {'password':'wrong'})[0], 401)
        self.assertEqual(self.request('/api/login', {'password':'wrong'})[0], 429)
        self.httpd.app.attempts.clear()

    def test_expired_session(self):
        cookie, _ = self.login()
        token = cookie.split('=',1)[1]
        csrf, _ = self.httpd.app.sessions[self.httpd.app.session_store.key(token)]
        self.httpd.app.sessions[self.httpd.app.session_store.key(token)] = csrf, time.time() - panel.TTL - 1
        self.assertEqual(self.request('/api/session', cookie=cookie)[0], 401)

    def test_docker_missing_is_readable(self):
        cookie, _ = self.login()
        with patch.object(panel.subprocess, 'run', side_effect=FileNotFoundError):
            status, data, _ = self.request('/api/status', cookie=cookie)
        self.assertEqual(status, 200)
        self.assertFalse(data['available'])


class PackagingTests(unittest.TestCase):
    def test_pinned_original_and_packaged_files(self):
        self.assertEqual(hashlib.sha256((ROOT/'vw_original.sh').read_bytes()).hexdigest(),
                         '756b783a388b087ff48af8db93273543a2ea2ea7009bdcfd50201e671d044aff')
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(['bash', str(ROOT/'install-vw-web.sh'), '--extract', directory], check=True, capture_output=True)
            for name in ('version.json','panel-cli.sh','vw_original.sh','vw_web.sh','web/server.py','web/session_store.py','web/manager.py','web/compose_yaml.py','web/proxy_environment.py','web/managed_proxy.py','web/existing_npm.py','web/updater.py','web/operations.py','web/remote_config.py','web/browser_oauth.py','web/backup_targets.py','web/backup_result.py','web/storage_presets.py','web/environment.py','web/env_catalog.json','web/tty_worker.py','web/static/index.html','web/static/app.js','web/static/style.css'):
                self.assertEqual((ROOT/name).read_bytes(), (Path(directory)/name).read_bytes())

    def test_web_installer_excludes_shell_features(self):
        installer = ROOT / 'install-vw-web-only.sh'
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(['bash', str(installer), '--extract', directory], check=True, capture_output=True)
            extracted = Path(directory)
            for name in ('vw_original.sh', 'vw_web.sh', 'web/tty_worker.py'):
                self.assertFalse((extracted / name).exists())
            html = (extracted / 'web/static/index.html').read_text()
            self.assertNotIn('id="advanced"', html)
            self.assertNotIn('id="feature-map"', html)
            manifest = json.loads((ROOT / 'release-web.json').read_text())
            version = json.loads((extracted / 'version.json').read_text())
            self.assertEqual(version['edition'], 'web')
            self.assertEqual(version['build_id'], manifest['build_id'])
            self.assertEqual(hashlib.sha256(installer.read_bytes()).hexdigest(), manifest['installer_sha256'])
        help_text = subprocess.check_output(['bash', str(installer), '--help'], text=True)
        self.assertNotIn('--original', help_text)
        self.assertNotEqual(subprocess.run(['bash', str(installer), '--original'], capture_output=True).returncode, 0)

    def test_corrupted_payload_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            broken = Path(directory)/'broken.sh'
            text = (ROOT/'install-vw-web.sh').read_text()
            start = text.index('\n__VW_PANEL_PAYLOAD__\n') + len('\n__VW_PANEL_PAYLOAD__\n')
            broken.write_text(text[:start] + ('A' if text[start]!='A' else 'B') + text[start+1:])
            result = subprocess.run(['bash',str(broken),'--extract',str(Path(directory)/'out')], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((Path(directory)/'out/web/server.py').exists())

    def test_actual_script_menu_with_fake_docker(self):
        # Run the real bundled script. Docker is stubbed so no containers or host files change.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            docker = path/'docker'
            docker.write_text('''#!/bin/bash
case "$1 $2" in
'compose version') echo 2.30.0 ;;
'version --format') echo 1.47 ;;
esac
exit 0
''')
            docker.chmod(0o755)
            term = panel.Terminal(ROOT/'vw_web.sh')
            with patch.dict(os.environ, {'HOME':directory, 'PATH':directory+':'+os.environ['PATH']}):
                try:
                    term.start()
                    eventually(lambda: panel.PROMPT in term.state()['output'], timeout=12)
                    output = term.state()['output']
                    self.assertNotIn('command not found', output)
                    self.assertNotIn('未找到命令', output)
                    self.assertIn('18)', output)
                    term.send('q\n')
                    eventually(lambda: term.state()['exit_code'] is not None)
                    self.assertEqual(term.state()['exit_code'], 0)
                    self.assertIn('再见', term.state()['output'])
                finally:
                    term.stop()


if __name__ == '__main__':
    unittest.main()

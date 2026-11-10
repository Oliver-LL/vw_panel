import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'web'))
from manager import Manager, read_vars, update_vars


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manager = Manager(self.root / 'state', self.root / 'memory.conf')
        self.deploy = self.root / 'deployment'
        self.data = dict(DOMAIN='https://vault.example.com', SERVER_ADMIN_EMAIL='admin@example.com',
                         directory=str(self.deploy), mode='host_ext', port='8080', network='vaultwarden_net',
                         SIGNUPS_ALLOWED=True, INVITATIONS_ALLOWED=True, SENDS_ALLOWED=False)

    def tearDown(self):
        if self.manager.logfile:
            self.manager.logfile.close()
        self.temp.cleanup()

    def configured(self):
        self.manager.save('core', self.data)
        return self.deploy / 'vaultwarden' / 'vaultwarden.env'

    def test_create_forms_and_preserve_unknown_settings_and_secrets(self):
        env = self.configured()
        update_vars(env, {'ADMIN_TOKEN': '$argon2id$secret', 'SMTP_PASSWORD': "a$#'\\b", 'UNKNOWN': 'untouched'})
        self.manager.save('mail', dict(SMTP_HOST='smtp.example.com', SMTP_PORT='465', SMTP_SECURITY='force_tls',
                                      SMTP_FROM='admin@example.com', SMTP_USERNAME='admin', SMTP_PASSWORD=''))
        values = read_vars(env)
        self.assertEqual(values['SMTP_PASSWORD'], "a$#'\\b")
        self.assertEqual(values['UNKNOWN'], 'untouched')
        self.assertEqual(values['ADMIN_TOKEN'], '$argon2id$secret')
        self.assertEqual(os.stat(env).st_mode & 0o777, 0o600)
        self.assertTrue(env.with_name(env.name + '.panel-backup').exists())
        public = json.dumps(self.manager.config())
        self.assertNotIn('$argon2id', public)
        self.assertNotIn("a$#", public)

    def test_new_backup_has_local_target_and_preserves_existing_remote(self):
        self.configured()
        data = dict(CRON='0 3 * * *', BACKUP_KEEP_DAYS='30', TIMEZONE='Asia/Shanghai', ZIP_PASSWORD='BackupPassword123!')
        self.manager.save('backup', data)
        config = self.deploy / 'vaultwarden-backup/vw-backup-config/rclone/rclone.conf'
        self.assertIn('type = local', config.read_text())
        config.write_text('[ExistingCloud]\ntype = webdav\n')
        env = self.deploy / 'vaultwarden-backup/backup.env'
        update_vars(env, {'RCLONE_REMOTE_NAME': 'ExistingCloud', 'RCLONE_REMOTE_DIR': '/private'})
        self.manager.save('backup', dict(data, ZIP_PASSWORD=''))
        self.assertIn('ExistingCloud', config.read_text())
        self.assertEqual(read_vars(env)['RCLONE_REMOTE_DIR'], '/private')
        self.assertEqual(read_vars(env)['ZIP_PASSWORD'], data['ZIP_PASSWORD'])

    def test_initial_backup_save_keeps_existing_remote_config_and_target(self):
        self.configured()
        backup = self.deploy / 'vaultwarden-backup'
        config = backup / 'vw-backup-config/rclone/rclone.conf'
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text('[R2Backup]\ntype = s3\nprovider = Cloudflare\nendpoint = https://%s.r2.cloudflarestorage.com\n' % ('a' * 32))
        env = backup / 'backup.env'
        env.write_text("RCLONE_REMOTE_NAME='R2Backup'\nRCLONE_REMOTE_DIR='/vw-backup/VaultBackup'\n")
        self.manager.save('backup', dict(CRON='0 3 * * *', BACKUP_KEEP_DAYS='30', TIMEZONE='Asia/Shanghai', ZIP_PASSWORD='BackupPassword123!'))
        text = config.read_text()
        self.assertIn('[R2Backup]', text)
        self.assertIn('provider = Cloudflare', text)
        self.assertIn('[BitwardenBackup]', text)
        self.assertIn('type = local', text)
        values = read_vars(env)
        self.assertEqual(values['RCLONE_REMOTE_NAME'], 'R2Backup')
        self.assertEqual(values['RCLONE_REMOTE_DIR'], '/vw-backup/VaultBackup')

    def test_initial_backup_save_avoids_local_name_collision_with_remote(self):
        self.configured()
        backup = self.deploy / 'vaultwarden-backup'
        config = backup / 'vw-backup-config/rclone/rclone.conf'
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text('[BitwardenBackup]\ntype = s3\nprovider = Cloudflare\nendpoint = https://%s.r2.cloudflarestorage.com\n' % ('b' * 32))
        self.manager.save('backup', dict(CRON='0 3 * * *', BACKUP_KEEP_DAYS='30', TIMEZONE='Asia/Shanghai', ZIP_PASSWORD='BackupPassword123!'))
        text = config.read_text()
        self.assertIn('[BitwardenBackup]\ntype = s3', text)
        self.assertIn('[PanelLocal]\ntype = local', text)
        values = read_vars(backup / 'backup.env')
        self.assertEqual(values['RCLONE_REMOTE_NAME'], 'PanelLocal')
        self.assertEqual(values['RCLONE_REMOTE_DIR'], '/vwp/backups/archive')

    def test_bare_vault_domain_normalized_and_validation_preserved(self):
        env=self.deploy/'vaultwarden'/'vaultwarden.env'
        for value,expected in [('vwp.777545.xyz','https://vwp.777545.xyz'),(' vault.example.com:8443/ ','https://vault.example.com:8443'),('https://vault.example.com','https://vault.example.com'),('//vault.example.com','https://vault.example.com')]:
            self.manager.save('core',dict(self.data,DOMAIN=value))
            self.assertEqual(read_vars(env)['DOMAIN'],expected)
        for value in ['', 'http://vault.example.com', 'vault.example.com/path', 'vault.example.com?x=1', 'user:pass@vault.example.com', 'vault.example.com:99999', 'vault.example.com#fragment']:
            with self.subTest(value=value),self.assertRaises(ValueError):
                self.manager.save('core',dict(self.data,DOMAIN=value))

    def test_permissions_save_does_not_modify_basic_or_secret_settings(self):
        env=self.configured();update_vars(env,{'ADMIN_TOKEN':'$argon2id$secret'})
        before=read_vars(env)
        self.manager.save('permissions',{'SIGNUPS_ALLOWED':False,'INVITATIONS_ALLOWED':False,'SENDS_ALLOWED':True,'DOMAIN':'https://ignored.example.com'})
        after=read_vars(env)
        self.assertEqual(after['DOMAIN'],before['DOMAIN']);self.assertEqual(after['ADMIN_TOKEN'],before['ADMIN_TOKEN'])
        self.assertEqual(after['SIGNUPS_ALLOWED'],'false');self.assertEqual(after['SENDS_ALLOWED'],'true')
        data={k:v for k,v in self.data.items() if k not in ('SIGNUPS_ALLOWED','INVITATIONS_ALLOWED','SENDS_ALLOWED')}
        self.manager.save('core',data)
        self.assertEqual(read_vars(env)['SIGNUPS_ALLOWED'],'false');self.assertEqual(read_vars(env)['SENDS_ALLOWED'],'true')
        self.assertEqual(self.manager.application_state()['core'],'pending')
        with self.assertRaises(ValueError):self.manager.save('permissions',{'SIGNUPS_ALLOWED':'false','INVITATIONS_ALLOWED':True,'SENDS_ALLOWED':False})

    def test_reject_invalid_values_and_existing_data(self):
        for changes in [dict(directory='/'), dict(directory='/opt/../etc'), dict(DOMAIN='https://bad.com\nrun'),
                        dict(network='x; touch /tmp/evil'), dict(port='90000')]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.manager.save('core', dict(self.data, **changes))
        folder = self.deploy / 'vaultwarden'
        folder.mkdir(parents=True)
        (folder / 'data').write_text('existing')
        with self.assertRaises(ValueError):
            self.manager.save('core', self.data)
        self.assertEqual((folder / 'data').read_text(), 'existing')

    def test_tasks_and_config_share_cross_process_lock(self):
        self.configured()
        with self.manager.exclusive():
            with self.assertRaises(ValueError):
                self.manager.save('core', self.data)
            with self.assertRaises(ValueError):
                self.manager.start('deploy', {})
        with self.assertRaises(ValueError):
            self.manager.start('arbitrary-shell', {})
        with self.assertRaises(ValueError):
            self.manager.start('stop', {})

    def wait_job(self):
        deadline = time.monotonic() + 3
        while self.manager.task()['state'] == 'running' and time.monotonic() < deadline:
            time.sleep(.01)
        return self.manager.task()

    def test_deploy_reports_failure_and_requires_hashed_admin_token(self):
        self.configured()
        self.manager.start('deploy', {})
        self.assertEqual(self.wait_job()['state'], 'failed')
        env = self.deploy / 'vaultwarden/vaultwarden.env'
        update_vars(env, {'ADMIN_TOKEN': '$argon2id$existing'})
        with patch.object(self.manager, '_command', side_effect=ValueError('image download failed')):
            self.manager.start('deploy', {})
            self.assertEqual(self.wait_job()['error'], 'image download failed')

    def test_admin_secret_uses_stdin_and_never_public_output(self):
        env = self.configured()
        calls = []
        def command(args, input_text=None, secret=False):
            calls.append((args, input_text, secret))
            return '$argon2id$v=19$m=65536,t=3,p=4$salt$hash\n'
        with patch.object(self.manager, '_command', side_effect=command):
            self.manager.start('admin-token', {'password': 'SecretPassword!123'})
            self.assertEqual(self.wait_job()['state'], 'success')
        self.assertTrue(calls[0][2])
        self.assertNotIn('SecretPassword!123', ' '.join(calls[0][0]))
        self.assertEqual(calls[0][1], 'SecretPassword!123')
        self.assertTrue(read_vars(env)['ADMIN_TOKEN'].startswith('$argon2id$'))
        self.assertNotIn('SecretPassword!123', json.dumps(self.manager.task()))

    @patch('manager.ensure_caddy_available')
    def test_new_caddy_config_and_no_overwrite_on_domain_change(self, caddy_available):
        self.manager.save('core', dict(self.data, mode='caddy'))
        caddy = self.deploy / 'caddy/Caddyfile'
        original = caddy.read_text()
        self.assertIn('vault.example.com', original)
        self.manager.save('core', dict(self.data, DOMAIN='https://other.example.com'))
        self.assertIn('other.example.com',caddy.read_text())
        self.assertNotIn('vault.example.com',caddy.read_text())
        self.assertEqual(original,caddy.with_name('Caddyfile.panel-backup').read_text())

    def test_webdav_validates_before_switching_and_preserves_other_remotes(self):
        self.configured()
        self.manager.save('backup', dict(CRON='0 3 * * *', BACKUP_KEEP_DAYS='30',
            TIMEZONE='Asia/Shanghai', ZIP_PASSWORD='BackupPassword123!'))
        config = self.deploy / 'vaultwarden-backup/vw-backup-config/rclone/rclone.conf'
        original = config.read_text()
        data = dict(url='https://dav.example.com/dav/', directory='/VaultBackup', user='me', password='WebdavSecret!')
        def failed(args, input_text=None, secret=False):
            if 'obscure' in args:
                return 'obscured_password'
            raise ValueError('network error')
        with patch.object(self.manager, '_command', side_effect=failed):
            self.manager.start('remote-save', data)
            self.assertEqual(self.wait_job()['state'], 'failed')
        self.assertEqual(config.read_text(), original)
        with patch.object(self.manager, '_command', side_effect=lambda args, *a, **k: 'obscured_password' if 'obscure' in args else ''):
            self.manager.start('remote-save', data)
            self.assertEqual(self.wait_job()['state'], 'success')
        self.assertIn('[BitwardenBackup]', config.read_text())
        self.assertIn('[PanelWebDAV]', config.read_text())
        public = json.dumps(self.manager.config()) + json.dumps(self.manager.task())
        self.assertNotIn('obscured_password', public)
        self.assertNotIn('WebdavSecret!', public)
        self.assertEqual(self.manager.config()['remote']['name'], 'PanelWebDAV')



if __name__ == '__main__':
    unittest.main()

class HTTPRoutesTests(unittest.TestCase):
    setUp = ManagerTests.setUp
    tearDown = ManagerTests.tearDown
    def test_authenticated_forms_csrf_and_head(self):
        import http.client
        import threading
        import importlib.util
        spec = importlib.util.spec_from_file_location('routes_panel', ROOT / 'web/server.py')
        panel = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(panel)
        self.manager.state.mkdir()
        (self.manager.state / 'panel.json').write_text(json.dumps({'port': 8787, 'host': '127.0.0.1', 'origins': []}))
        (self.manager.state/'credentials.json').write_text(json.dumps({'salt':'fixture','hash':'fixture'}))
        app = panel.Application(self.manager.state)
        app.manager = self.manager
        server = panel.ThreadingHTTPServer(('127.0.0.1', 0), panel.Handler)
        port = server.server_port
        app.config['port'] = port
        app.sessions[app.session_store.key('fixture')] = ('csrf-fixture', time.time())
        server.app = app
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        def request(method, path, data=None, csrf=True, authenticated=True):
            headers = {'Origin': 'http://127.0.0.1:%s' % port, 'Content-Type': 'application/json'}
            if authenticated:
                headers['Cookie'] = 'vw_session=fixture'
            if csrf:
                headers['X-CSRF-Token'] = 'csrf-fixture'
            conn.request(method, path, json.dumps(data) if data is not None else None, headers)
            response = conn.getresponse()
            body = response.read()
            return response.status, body
        try:
            self.assertEqual(request('GET', '/api/config', authenticated=False)[0], 401)
            with patch.object(panel, 'detect_environment', return_value={'available': True, 'proxies': []}) as detection:
                self.assertEqual(request('GET', '/api/proxy/environment', authenticated=False)[0], 401)
                detection.assert_not_called()
                self.assertEqual(request('GET', '/api/proxy/environment')[0], 200)
                detection.assert_called_once()
            with patch.object(app.manager, 'npm_credentials', return_value={'email':'admin@example.com','password':'fixture-secret','port':8181}) as credentials:
                self.assertEqual(request('POST','/api/proxy/credentials',{},authenticated=False)[0],401)
                self.assertEqual(request('POST','/api/proxy/credentials',{},csrf=False)[0],403)
                credentials.assert_not_called()
                status,body=request('POST','/api/proxy/credentials',{})
                self.assertEqual(status,200);self.assertEqual(json.loads(body)['password'],'fixture-secret')
            self.assertEqual(request('POST', '/api/config/core', self.data, csrf=False)[0], 403)
            self.assertFalse(self.deploy.exists())
            self.assertEqual(request('POST', '/api/config/core', self.data)[0], 200)
            status, body = request('GET', '/api/config')
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)['configured'])
            remote_file=self.manager.paths()[1]/'vw-backup-config/rclone/rclone.conf';remote_file.parent.mkdir(parents=True,exist_ok=True)
            remote_file.write_text('[R2]\ntype = s3\naccess_key_id = private-access-fixture\nsecret_access_key = private-secret-fixture\n[Other]\ntype = s3\nsecret_access_key = other-private-secret\n')
            original=remote_file.read_text()
            self.assertEqual(request('POST','/api/storage/config',{'name':'R2'},authenticated=False)[0],401)
            self.assertEqual(request('POST','/api/storage/config',{'name':'R2'},csrf=False)[0],403)
            status,private=request('POST','/api/storage/config',{'name':'R2'});self.assertEqual(status,200)
            self.assertEqual(json.loads(private)['fields']['secret_access_key'],'private-secret-fixture')
            self.assertNotIn(b'other-private-secret',private)
            self.assertEqual(request('POST','/api/storage/config',{'name':'../R2'})[0],409)
            self.assertEqual(request('POST','/api/storage/config',{'name':'Missing'})[0],409)
            self.assertNotIn(b'private-secret-fixture',request('GET','/api/config')[1])
            self.assertEqual(remote_file.read_text(),original)

            with patch.object(app.manager,'storage_providers',return_value=[{'name':'drive','description':'Google Drive'}]) as providers:
                self.assertEqual(request('GET','/api/storage/providers',authenticated=False)[0],401)
                providers.assert_not_called()
                self.assertEqual(request('GET','/api/storage/providers')[0],200)
                providers.assert_called_once()

            with patch('remote_config.cancel',return_value={'ok':True}) as cancel:
                self.assertEqual(request('POST','/api/storage/wizard/cancel',{'job_id':'fixture'},authenticated=False)[0],401)
                self.assertEqual(request('POST','/api/storage/wizard/cancel',{'job_id':'fixture'},csrf=False)[0],403)
                cancel.assert_not_called()
                self.assertEqual(request('POST','/api/storage/wizard/cancel',{'job_id':'fixture'})[0],200)
                cancel.assert_called_once_with(app.manager,'fixture')

            headers={'Origin':'http://127.0.0.1:%s'%port,'Content-Type':'application/octet-stream','Cookie':'vw_session=fixture','X-CSRF-Token':'csrf-fixture'}
            conn.request('POST','/api/backup/upload?name=fixture.zip',b'archive fixture',headers)
            uploaded=conn.getresponse();self.assertEqual(uploaded.status,200)
            name=json.loads(uploaded.read())['file']
            status,body=request('GET','/api/backups/download?file='+name)
            self.assertEqual(status,200);self.assertEqual(body,b'archive fixture')
            self.assertEqual(request('GET','/api/backups/download?file=../private.zip')[0],404)
            self.assertEqual(request('HEAD', '/')[0], 200)
            self.assertEqual(request('POST', '/api/task/start', {'action': ['bad']})[0], 409)
            with patch.object(app.updates,'check',return_value={'installed':{'version':'0.5.0'},'available':False}):
                self.assertEqual(request('GET','/api/updates',authenticated=False)[0],401)
                self.assertEqual(request('GET','/api/updates')[0],200)
            with patch.object(app.updates,'start',return_value={'state':'upgrading'}) as start:
                self.assertEqual(request('POST','/api/updates/start',{'build_id':'b'*64,'confirmed':True},csrf=False)[0],403)
                start.assert_not_called()
                with patch.object(app.terminal,'state',return_value={'running':True}):
                    self.assertEqual(request('POST','/api/updates/start',{'build_id':'b'*64,'confirmed':True})[0],409)
                self.assertEqual(request('POST','/api/updates/start',{'build_id':'b'*64,'confirmed':True})[0],200)
            with patch.object(app.updates,'busy',return_value=True):
                self.assertEqual(request('POST','/api/config/core',self.data)[0],409)
                self.assertEqual(request('POST','/api/task/start',{'action':'deploy'})[0],409)

        finally:
            conn.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

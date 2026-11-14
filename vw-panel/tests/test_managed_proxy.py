from compose_test_utils import load_compose
import contextlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'web'))
from manager import Manager, read_vars
from managed_proxy import ensure_proxy_ports, https_nginx, MARKER

class ManagedProxyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.manager=Manager(self.root/'state',self.root/'memory')
        self.deploy=self.root/'deployment';self.calls=[];self.events=[]
        self.data=dict(DOMAIN='https://vault.example.com',SERVER_ADMIN_EMAIL='admin@example.com',directory=str(self.deploy),
            mode='npm',network='vaultwarden_net',port=8080,NPM_ADMIN_PORT=8181,
            SIGNUPS_ALLOWED=False,INVITATIONS_ALLOWED=True,SENDS_ALLOWED=False)
        self.guard=patch('managed_proxy.ensure_proxy_ports');self.guard.start();self.addCleanup(self.guard.stop)
        self.emitter=patch.object(self.manager,'emit',side_effect=self.events.append);self.emitter.start();self.addCleanup(self.emitter.stop)
    def tearDown(self):
        if self.manager.logfile:self.manager.logfile.close()
        self.temp.cleanup()
    def configure(self,kind='npm',**extra):
        self.manager.save('core',dict(self.data,mode=kind,**extra))
        return self.deploy/kind
    def command(self,args,**kwargs):
        self.calls.append(args)
        if args[:2]==['docker','inspect']:return 'true'
        return ''
    def test_npm_creation_and_secret_roundtrip_without_public_leak(self):
        password='P$a\\s"s$$word!12345'
        folder=self.configure(NPM_ADMIN_PASSWORD=password)
        compose=load_compose(folder/'docker-compose.yml')
        self.assertEqual(compose['services']['npm']['container_name'], 'nginx-proxy-manager')
        self.assertEqual(list(compose['services']['npm']['networks']), ['nginx-proxy-manager_default'])
        core = load_compose(self.deploy/'vaultwarden/docker-compose.yml')
        self.assertEqual(list(core['services']['vaultwarden']['networks']), ['nginx-proxy-manager_default'])
        self.assertEqual(self.manager.npm_credentials()['password'],password)
        self.assertEqual(compose['services']['npm']['environment']['INITIAL_ADMIN_PASSWORD'],password.replace('$','$$'))
        self.assertNotIn(password,json.dumps(self.manager.config()))
        self.assertEqual((folder/'credentials.json').stat().st_mode&0o777,0o600)
        memory=read_vars(self.manager.memory)
        self.assertEqual(memory['PROXY_MODE'],'docker_ext');self.assertEqual(memory['SKIP_CADDY'],'true')
        self.assertEqual(self.manager.config()['mode'],'npm')
        self.assertEqual(self.manager.config()['proxy']['admin_port'],8181)
        self.manager.save('core',dict(self.data,NPM_ADMIN_PASSWORD='',DOMAIN='https://changed.example.com'))
        self.assertEqual(self.manager.npm_credentials()['password'],password)
        self.assertEqual(self.manager.proxy_settings()['domain'],'changed.example.com')
    def test_validation_before_files_and_draft_creation(self):
        for changes in ({'SERVER_ADMIN_EMAIL':''},{'NPM_ADMIN_PORT':80},{'NPM_ADMIN_PASSWORD':'short'}, {'DOMAIN':'https://vault.example.com:8443'}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):self.manager.save('core',dict(self.data,**changes))
            self.assertFalse(self.deploy.exists())
    def test_nginx_plan_and_domain_save_leave_active_config_unchanged(self):
        folder=self.configure('nginx');active=(folder/'conf.d/site.conf').read_text()
        self.assertNotIn('listen 443',active)
        self.assertIn('proxy_set_header Upgrade',(folder/'desired.conf').read_text())
        compose=load_compose(folder/'docker-compose.yml')
        self.assertEqual(compose['services']['nginx']['container_name'], 'nginx')
        self.assertEqual(list(compose['services']['nginx']['networks']), ['nginx_default'])
        self.assertEqual(compose['services']['renew']['container_name'], 'certbot')
        self.assertEqual(compose['services']['certbot']['profiles'],['certificate'])
        self.assertIn('certbot renew',compose['services']['renew']['command'][0])
        self.manager.save('core',dict(self.data,mode='nginx',DOMAIN='https://changed.example.com'))
        self.assertEqual((folder/'conf.d/site.conf').read_text(),active)
        self.assertIn('changed.example.com',(folder/'desired.conf').read_text())
    def test_nginx_certificate_before_tls_reload_and_background_renewal(self):
        folder=self.configure('nginx')
        def command(args,**kwargs):
            result=self.command(args,**kwargs)
            if 'certonly' in args:
                self.assertNotIn('listen 443',(folder/'conf.d/site.conf').read_text())
                cert=folder/'letsencrypt/live/vault.example.com';cert.mkdir(parents=True)
                (cert/'fullchain.pem').write_text('fixture');(cert/'privkey.pem').write_text('fixture')
            return result
        with patch.object(self.manager,'_command',side_effect=command):self.manager.deploy_managed_proxy()
        self.assertIn('listen 443 ssl',(folder/'conf.d/site.conf').read_text())
        self.assertTrue(any(args[-3:]==['up','-d','renew'] for args in self.calls))
        self.assertTrue(any(args[-3:]==['nginx','nginx','-t'] for args in self.calls))
    def test_nginx_acme_failure_restores_previous_configuration(self):
        folder=self.configure('nginx');previous=https_nginx('previous.example.com');(folder/'conf.d/site.conf').write_text(previous)
        def command(args,**kwargs):
            if 'certonly' in args:raise ValueError('ACME failed')
            return self.command(args,**kwargs)
        with patch.object(self.manager,'_command',side_effect=command),self.assertRaisesRegex(ValueError,'ACME failed'):
            self.manager.deploy_managed_proxy()
        self.assertEqual((folder/'conf.d/site.conf').read_text(),previous)
        self.assertFalse(any(args[-3:]==['up','-d','renew'] for args in self.calls))
    def npm_fixture(self,path,data=None,token=None,method=None,timeout=15):
        self.calls.append((path,data,method))
        if path=='nginx/proxy-hosts' and data is None:return self.hosts
        if path=='nginx/certificates' and data is None:return self.certificates
        if path=='nginx/certificates':
            self.certificates=[{'id':3,'provider':'letsencrypt','domain_names':['vault.example.com'],'expires_on':'2099-01-01T00:00:00Z'}]
            return self.certificates[0]
        if path.startswith('nginx/proxy-hosts'):
            self.assertTrue(data['ssl_forced']);self.assertTrue(data['allow_websocket_upgrade']);self.assertEqual(data['forward_host'],'vaultwarden')
            self.hosts=[{'id':4,**data}];return self.hosts[0]
        raise AssertionError(path)
    def test_npm_configuration_retries_without_duplicate_sites_or_certificates(self):
        folder=self.configure();self.hosts=[];self.certificates=[]
        with patch.object(self.manager,'_npm_token',return_value='token'),patch.object(self.manager,'_npm_request',side_effect=self.npm_fixture):
            self.manager.configure_npm();self.manager.configure_npm()
        self.assertEqual(sum(path=='nginx/certificates' and data is not None for path,data,method in self.calls),1)
        self.assertEqual(sum(path=='nginx/proxy-hosts' and data is not None for path,data,method in self.calls),1)
        self.assertTrue(any(path=='nginx/proxy-hosts/4' and method=='PUT' for path,data,method in self.calls))
        self.assertEqual(json.loads((folder/'route-state.json').read_text())['host_id'],4)
    def test_npm_does_not_overwrite_an_unrecorded_site(self):
        self.configure();self.hosts=[{'id':7,'domain_names':['vault.example.com'],'forward_host':'other'}];self.certificates=[]
        with patch.object(self.manager,'_npm_token',return_value='token'),patch.object(self.manager,'_npm_request',side_effect=self.npm_fixture),self.assertRaisesRegex(ValueError,'已存在'):
            self.manager.configure_npm()
        self.assertFalse(any(data is not None for path,data,method in self.calls))
    def test_generated_credentials_survive_core_reinstall(self):
        folder=self.configure();password=self.manager.npm_credentials()['password']
        import shutil
        shutil.rmtree(self.deploy/'vaultwarden')
        self.manager.save('core',self.data)
        self.assertEqual(self.manager.npm_credentials()['password'],password)
    def test_removal_preserves_proxy_by_default_and_explicit_removal_is_bounded(self):
        folder=self.configure()
        with patch.object(self.manager,'_command',side_effect=self.command):
            self.manager.remove_services({'scope':'core','delete_data':False})
        self.assertTrue(folder.exists());self.assertFalse(any(str(folder) in args for args in self.calls))
        self.calls.clear()
        with patch.object(self.manager,'_command',side_effect=self.command):
            self.manager.remove_services({'scope':'all','delete_data':True,'remove_proxy':True})
        self.assertFalse(folder.exists());self.assertEqual(self.manager.config()['proxy'],{})
        self.assertTrue(any(str(folder) in args and args[-1]=='down' for args in self.calls))
    def test_npm_log_output_redacts_initial_admin_password(self):
        self.configure();password=self.manager.npm_credentials()['password'];self.manager.logfile=tempfile.TemporaryFile('w+b')
        def run(args,**kwargs):
            kwargs['stdout'].write(('Creating user password: '+password+'\nCreating a new user: old@example.com with password: OldBootstrapSecret123!').encode());return SimpleNamespace(returncode=0)
        with patch('manager.subprocess.run',side_effect=run):raw=self.manager._command(['docker','logs','nginx-proxy-manager'])
        self.assertNotIn(password,raw);self.assertNotIn('OldBootstrapSecret123!',raw);self.manager.logfile.seek(0);self.assertNotIn(password,self.manager.logfile.read().decode())



class ManagedProxyIntegrationTests(unittest.TestCase):
    setUp=ManagedProxyTests.setUp
    tearDown=ManagedProxyTests.tearDown
    configure=ManagedProxyTests.configure
    command=ManagedProxyTests.command
    def test_managed_network_is_created_and_success_records_proxy_revision(self):
        from manager import update_vars
        self.configure()
        update_vars(self.deploy/'vaultwarden/vaultwarden.env',{'ADMIN_TOKEN':'$argon2id$fixture'})
        with patch.object(self.manager,'_command',side_effect=self.command),patch.object(self.manager,'deploy_managed_proxy') as deploy,patch('manager.existing_network',side_effect=AssertionError('new managed network must not be treated as external')),patch('manager.ensure_proxy_ports'),patch('manager.subprocess.run',return_value=SimpleNamespace(returncode=1)):
            self.manager._run('deploy',{},contextlib.nullcontext())
        self.assertEqual(self.manager.task()['state'],'success');deploy.assert_called_once_with(upgrade=False)
        self.assertIn(['docker','network','create','nginx-proxy-manager_default'],self.calls)
        self.assertEqual(self.manager.config()['application']['core'],'applied')
    def test_nginx_logs_include_certificate_renewal_service(self):
        self.configure('nginx')
        with patch.object(self.manager,'_command',side_effect=self.command),patch('proxy_environment.detect_environment',return_value={'proxies':[{'id':'a'*12,'name':'nginx','kind':'Nginx','source':'docker','running':True,'networks':['nginx_default']}]}):self.manager.run_extended('proxy-logs',{})
        self.assertEqual([cmd[-1] for cmd in self.calls],['a'*12,'certbot'])

class ManagedProxyOwnershipTests(unittest.TestCase):
    def test_running_nginx_does_not_hide_foreign_renewal_container(self):
        folder=Path('/tmp/panel-owned-nginx')
        def inspect(args):
            source=str(folder/'conf.d') if args[-1]=='nginx' else '/unrelated/certificates'
            return SimpleNamespace(returncode=0,stdout=json.dumps({'running':True,'mounts':[{'Source':source}]}))
        with patch('managed_proxy.docker',side_effect=inspect),self.assertRaisesRegex(ValueError,'certbot'):
            ensure_proxy_ports('nginx',folder)

class NPMHTTPTests(unittest.TestCase):
    setUp=ManagedProxyTests.setUp
    tearDown=ManagedProxyTests.tearDown
    configure=ManagedProxyTests.configure
    def test_npm_api_wire_protocol_and_credentials_are_not_logged(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        password='Wire$Password123!'
        requests=[];remote={'hosts':[],'certificates':[]}
        class API(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def reply(self,data):
                raw=json.dumps(data).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            def do_GET(self):
                if self.headers.get('Authorization')!='Bearer wire-token':self.send_error(401);return
                self.reply(remote['hosts'] if self.path.endswith('proxy-hosts') else remote['certificates'])
            def do_POST(self):
                data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append((self.path,data))
                if self.path=='/api/tokens':
                    if data!={'identity':'admin@example.com','secret':password}:self.send_error(401);return
                    self.reply({'token':'wire-token'});return
                if self.headers.get('Authorization')!='Bearer wire-token':self.send_error(401);return
                if self.path=='/api/nginx/certificates':
                    remote['certificates']=[{'id':3,**data,'expires_on':'2099-01-01T00:00:00Z'}];self.reply(remote['certificates'][0]);return
                remote['hosts']=[{'id':5,**data,'meta':{'nginx_online':True}}];self.reply(remote['hosts'][0])
        server=ThreadingHTTPServer(('127.0.0.1',0),API);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            self.configure(NPM_ADMIN_PORT=server.server_port,NPM_ADMIN_PASSWORD=password)
            self.manager.configure_npm()
            self.assertTrue(remote['hosts'][0]['ssl_forced']);self.assertEqual(remote['hosts'][0]['certificate_id'],3)
            self.assertEqual([p for p,data in requests],['/api/tokens','/api/nginx/certificates','/api/nginx/proxy-hosts'])
            self.assertNotIn(password,' '.join(self.events))
        finally:server.shutdown();server.server_close();thread.join()

class ComposeFileTests(unittest.TestCase):
    def test_generated_files_with_real_compose_when_available(self):
        import shutil, subprocess
        docker=shutil.which('docker')
        if not docker:self.skipTest('Docker Compose is not installed')
        version=subprocess.run([docker,'compose','version'],capture_output=True,text=True)
        if version.returncode:self.skipTest('Docker Compose is not installed')
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp)
            for kind in ('npm','nginx'):
                m=Manager(base/kind/'state',base/kind/'memory')
                data=dict(DOMAIN='https://vault.example.com',SERVER_ADMIN_EMAIL='admin@example.com',directory=str(base/kind/'deployment'),
                    mode=kind,network='vaultwarden_net',port=8080,NPM_ADMIN_PORT=8181,NPM_ADMIN_PASSWORD='Compose$Secret$$123!',
                    SIGNUPS_ALLOWED=False,INVITATIONS_ALLOWED=True,SENDS_ALLOWED=False)
                with patch('managed_proxy.ensure_proxy_ports'):m.save('core',data)
                result=subprocess.run([docker,'compose','-f',str(m.proxy_folder()/'docker-compose.yml'),'config','--format','json'],capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr)
                normalized=json.loads(result.stdout)
                # Compose config escapes literal $ again so its output can be used as another Compose file.
                if kind=='npm':self.assertEqual(normalized['services']['npm']['environment']['INITIAL_ADMIN_PASSWORD'].replace('$$','$'),data['NPM_ADMIN_PASSWORD'])
                else:
                    for name in ('nginx','renew'):
                        cmd=normalized['services'][name]['command'];script=(cmd[-1] if name=='nginx' else cmd[0]).replace('$$','$')
                        checked=subprocess.run(['/bin/sh','-n','-c',script],capture_output=True,text=True)
                        self.assertEqual(checked.returncode,0,checked.stderr)

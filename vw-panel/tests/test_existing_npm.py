import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import test_manager as fixtures
from existing_npm import ExistingNPM
from manager import update_vars

class ExistingNPMTests(unittest.TestCase):
    setUp=fixtures.ManagerTests.setUp
    tearDown=fixtures.ManagerTests.tearDown
    configured=fixtures.ManagerTests.configured
    wait_job=fixtures.ManagerTests.wait_job

    def prepare(self):
        self.configured()
        self.target='a'*12
        update_vars(self.manager.memory,{'PROXY_MODE':'docker_ext','PANEL_PROXY_TARGET':self.target,'DOCKER_NET':'npm_default'})
        self.manager.note_applied('core')
        self.choices={'proxies':[{'id':self.target,'kind':'Nginx Proxy Manager','running':True,'networks':['npm_default']}]}
        self.data={'target':self.target,'email':'owner@example.com','password':'NpmPrivatePassword!'}
        self.guard=patch('proxy_environment.detect_environment',return_value=self.choices);self.guard.start();self.addCleanup(self.guard.stop)
        def command(args,**kwargs):
            return json.dumps({'npm_default':{'IPAddress':'172.20.0.2'}}) if '.NetworkSettings.Networks' in ' '.join(args) else 'true'
        self.command=patch.object(self.manager,'_command',side_effect=command);self.command.start();self.addCleanup(self.command.stop)
        self.events=[];self.emitter=patch.object(self.manager,'emit',side_effect=self.events.append);self.emitter.start();self.addCleanup(self.emitter.stop)
        self.calls=[];self.hosts=[]

    def request(self,path,data=None,token=None,method=None,timeout=15):
        self.calls.append((path,data,method))
        if path=='tokens':return {'token':'private-token'}
        if path=='nginx/proxy-hosts' and data is None:return self.hosts
        if path=='nginx/certificates' and data is None:return []
        if path=='nginx/certificates':return {'id':7}
        return {'id':9,'meta':{'nginx_online':True}}

    def test_creates_route_without_credentials_on_disk(self):
        self.prepare();adapter=ExistingNPM(self.manager,self.data)
        self.assertEqual(adapter._npm_api_base(),'http://172.20.0.2:81/api/')
        with patch.object(adapter,'_npm_request',side_effect=self.request):adapter.configure()
        route=next(data for path,data,method in self.calls if path=='nginx/proxy-hosts' and data)
        self.assertEqual(route['forward_host'],'vaultwarden');self.assertEqual(route['forward_port'],80)
        self.assertTrue(route['ssl_forced']);self.assertTrue(route['allow_websocket_upgrade'])
        state=adapter.folder/'route-state.json';self.assertEqual(os.stat(state).st_mode&0o777,0o600)
        self.assertNotIn(self.data['password'],state.read_text());self.assertNotIn('private-token',state.read_text())
        self.assertEqual(json.loads(state.read_text())['network'],'npm_default')
        self.assertEqual(adapter.password,'')
        record=self.manager.config()['external_proxy']
        self.assertTrue(record['recorded']);self.assertEqual(record['email'],self.data['email'])
        self.assertNotIn(self.data['password'],json.dumps(record))
        old=json.loads(state.read_text());old.pop('email');state.write_text(json.dumps(old))
        legacy=self.manager.config()['external_proxy'];self.assertTrue(legacy['recorded']);self.assertEqual(legacy['email'],'')
        update_vars(self.manager.memory,{'PANEL_PROXY_TARGET':''});(self.manager.state/'existing-npm'/'last-target.json').unlink()
        self.assertTrue(self.manager.config()['external_proxy']['recorded'])


    def test_foreign_same_domain_stops_before_writes(self):
        self.prepare();self.hosts=[{'id':3,'domain_names':['vault.example.com']}]
        adapter=ExistingNPM(self.manager,self.data)
        with patch.object(adapter,'_npm_request',side_effect=self.request),self.assertRaisesRegex(ValueError,'已存在'):adapter.configure()
        self.assertFalse(any(data for path,data,method in self.calls if path!='tokens'))

    def test_recorded_host_changed_outside_panel_is_preserved(self):
        self.prepare();adapter=ExistingNPM(self.manager,self.data)
        (adapter.folder/'route-state.json').write_text(json.dumps({'host_id':9,'domain':'vault.example.com','network':'npm_default'}))
        self.hosts=[{'id':9,'domain_names':['other.example.com'],'forward_host':'another','forward_port':8080}]
        with patch.object(adapter,'_npm_request',side_effect=self.request),self.assertRaisesRegex(ValueError,'被修改'):adapter.configure()
        self.assertFalse(any(data for path,data,method in self.calls if path!='tokens'))

    def test_missing_network_or_wrong_target_rejected(self):
        self.prepare();self.choices['proxies'][0]['networks']=[]
        with self.assertRaisesRegex(ValueError,'网络'):ExistingNPM(self.manager,self.data)
        with self.assertRaisesRegex(ValueError,'不一致'):ExistingNPM(self.manager,{**self.data,'target':'b'*12})

    def test_failed_certificate_does_not_create_site(self):
        self.prepare();adapter=ExistingNPM(self.manager,self.data)
        def request(path,*args,**kwargs):
            if path=='nginx/certificates':raise ValueError('证书申请失败')
            return self.request(path,*args,**kwargs)
        with patch.object(adapter,'_npm_request',side_effect=request),self.assertRaises(ValueError):adapter.configure()
        self.assertFalse(any(path=='nginx/proxy-hosts' and data for path,data,method in self.calls))
        self.assertEqual(adapter.password,'')

    def test_task_validation_does_not_expose_credentials(self):
        self.prepare()
        for bad in [{'target':'http://evil','email':'a@b.com','password':'x'}, {**self.data,'password':''},{**self.data,'email':'bad'}]:
            with self.assertRaises(ValueError):self.manager.prepare_extended('existing-npm',bad)
        with patch('existing_npm.ExistingNPM.configure',return_value=None):
            self.manager.start('existing-npm',self.data)
            job=self.wait_job();self.assertEqual(job['state'],'success')
            self.assertNotIn(self.data['password'],json.dumps(job))

    def test_real_http_protocol_and_redirect_rejection(self):
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        import threading
        self.prepare();adapter=ExistingNPM(self.manager,self.data);seen=[];redirect=[False]
        class API(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):self.handle_api()
            def do_POST(self):self.handle_api()
            def handle_api(self):
                data=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))) or '{}')
                seen.append((self.path,data,self.headers.get('Authorization')))
                if redirect[0]:self.send_response(302);self.send_header('Location','/leaked');self.end_headers();return
                if self.path=='/api/tokens':response={'token':'private-token'}
                elif self.command=='GET':response=[]
                elif self.path=='/api/nginx/certificates':response={'id':7}
                else:response={'id':9,'meta':{'nginx_online':True}}
                raw=json.dumps(response).encode();self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        server=ThreadingHTTPServer(('127.0.0.1',0),API);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with patch.object(adapter,'_npm_api_base',return_value='http://127.0.0.1:%s/api/'%server.server_port):
                adapter.configure()
                cert=next(data for path,data,auth in seen if path=='/api/nginx/certificates' and data)
                self.assertEqual(cert['meta']['letsencrypt_email'],self.data['email'])
                self.assertTrue(cert['meta']['letsencrypt_agree'])
                self.assertTrue(all(auth=='Bearer private-token' for path,data,auth in seen if path!='/api/tokens'))
                redirect[0]=True;seen.clear()
                with self.assertRaises(ValueError):adapter._npm_request('tokens',{'identity':self.data['email'],'secret':'RedirectSecret!'})
                self.assertEqual([path for path,data,auth in seen],['/api/tokens'])
        finally:server.shutdown();server.server_close();thread.join()

    def test_pending_core_or_detached_main_network_prevents_npm_writes(self):
        self.prepare();app,_=self.manager.paths()
        update_vars(app/'vaultwarden.env',{'DOMAIN':'https://changed.example.com'})
        with self.assertRaisesRegex(ValueError,'先应用'):ExistingNPM(self.manager,self.data)
        self.manager.note_applied('core')
        def command(args,**kwargs):
            if '.NetworkSettings.Networks' in ' '.join(args):return '{}' if args[-1]=='vaultwarden' else json.dumps({'npm_default':{'IPAddress':'172.20.0.2'}})
            return 'true'
        with patch.object(self.manager,'_command',side_effect=command),self.assertRaisesRegex(ValueError,'尚未接入'):ExistingNPM(self.manager,self.data)

import contextlib
import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from unittest.mock import patch
from existing_npm import ExistingNPM
from managed_proxy import NPMAPIError
import test_existing_npm as fixtures

class NPMCompatibilityTests(unittest.TestCase):
    setUp=fixtures.ExistingNPMTests.setUp
    tearDown=fixtures.ExistingNPMTests.tearDown
    prepare=fixtures.ExistingNPMTests.prepare
    configured=fixtures.ExistingNPMTests.configured
    wait_job=fixtures.ExistingNPMTests.wait_job

    @contextlib.contextmanager
    def api(self,mode):
        if not hasattr(self,'_core_data'):self._core_data=dict(self.data)
        self.data=dict(self._core_data)
        self.prepare();adapter=ExistingNPM(self.manager,self.data);seen=[];secret=self.data['password']
        class API(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):self.api()
            def do_POST(self):self.api()
            def do_PUT(self):self.api()
            def api(self):
                data=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))) or '{}')
                seen.append((self.path,data,self.command));status=200
                if self.path=='/api/':response={'version':{'major':2,'minor':12 if mode=='legacy' else 13 if mode=='fork' else 16}} if mode!='unknown-legacy' else {}
                elif self.path=='/api/tokens':
                    if mode=='auth':status=401;response={'error':{'message':'Invalid credentials '+secret}}
                    elif mode=='redirect':self.send_response(302);self.send_header('Location','/leaked');self.end_headers();return
                    elif mode=='json':self.send_response(200);self.end_headers();self.wfile.write(b'<html>not json</html>');return
                    else:response={'token':'PrivateBearerToken'}
                elif self.path=='/api/nginx/certificates' and self.command=='GET':
                    if mode=='permission':status=403;response={'error':{'message':'not allowed'}}
                    else:response=[]
                elif self.path=='/api/nginx/proxy-hosts' and self.command=='GET':response=[]
                elif self.path=='/api/nginx/certificates':
                    meta=data['meta'];legacy='letsencrypt_email' in meta
                    if mode=='dns':status=400;response={'error':{'message':'DNS problem: NXDOMAIN '+secret}}
                    elif mode in ('legacy','unknown-legacy') and not legacy:status=400;response={'error':{'message':"meta must have required property 'letsencrypt_email'"}}
                    elif mode not in ('legacy','unknown-legacy') and legacy:status=400;response={'error':{'message':'meta must NOT have additional properties'}}
                    else:response={'id':7}
                else:response={'id':9,'meta':{'nginx_online':True}}
                raw=json.dumps(response).encode();self.send_response(status);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        server=ThreadingHTTPServer(('127.0.0.1',0),API);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with patch.object(adapter,'_npm_api_base',return_value='http://127.0.0.1:%s/api/'%server.server_port):yield adapter,seen
        finally:server.shutdown();server.server_close();thread.join()

    def test_modern_npm_accepts_existing_account_and_modern_certificate_parameters(self):
        with self.api('modern') as (adapter,seen):adapter.configure()
        payloads=[d for p,d,m in seen if p=='/api/nginx/certificates' and m=='POST']
        self.assertEqual(len(payloads),1);self.assertEqual(payloads[0]['meta'],{'dns_challenge':False})
        self.assertTrue(any('登录已通过' in message for message in self.events))
        self.assertEqual(json.loads((adapter.folder/'route-state.json').read_text())['host_id'],9)

    def test_legacy_certificate_parameters_selected_by_reported_version(self):
        with self.api('legacy') as (adapter,seen):adapter.configure()
        payloads=[d for p,d,m in seen if p=='/api/nginx/certificates' and m=='POST']
        self.assertEqual(len(payloads),1);self.assertEqual(payloads[0]['meta']['letsencrypt_email'],self.data['email'])

    def test_schema_rejection_only_switches_format_once(self):
        for mode in ('fork','unknown-legacy'):
            with self.subTest(mode=mode),self.api(mode) as (adapter,seen):
                adapter.configure()
                payloads=[d for p,d,m in seen if p=='/api/nginx/certificates' and m=='POST']
                self.assertEqual(len(payloads),2)
                self.assertNotEqual('letsencrypt_email' in payloads[0]['meta'],'letsencrypt_email' in payloads[1]['meta'])

    def test_certificate_dns_failure_never_blames_password_or_retries(self):
        with self.api('dns') as (adapter,seen):
            with self.assertRaises(NPMAPIError) as result:adapter.configure()
        message=str(result.exception);self.assertIn('登录已通过',message);self.assertIn('DNS',message);self.assertIn('HTTP 400',message)
        self.assertNotIn(self.data['password'],message);self.assertNotIn(self.data['password'],' '.join(self.events))
        self.assertEqual(sum(p=='/api/nginx/certificates' and m=='POST' for p,d,m in seen),1)
        self.assertFalse(any(p=='/api/nginx/proxy-hosts' and m=='POST' for p,d,m in seen))

    def test_auth_permissions_json_and_redirect_failures_are_distinct_and_redacted(self):
        for mode,text in [('auth','登录未通过'),('permission','没有此操作权限'),('json','有效 JSON'),('redirect','重定向')]:
            with self.subTest(mode=mode),self.api(mode) as (adapter,seen):
                with self.assertRaises(NPMAPIError) as result:adapter.configure()
                self.assertIn(text,str(result.exception));self.assertNotIn(self.data['password'],str(result.exception))
                self.assertFalse(any(p=='/leaked' for p,d,m in seen))

    def test_connection_failure_is_not_reported_as_password_failure(self):
        self.prepare();adapter=ExistingNPM(self.manager,self.data)
        with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
        with patch.object(adapter,'_npm_api_base',return_value='http://127.0.0.1:%s/api/'%port):
            with self.assertRaisesRegex(NPMAPIError,'无法连接'):adapter.configure()

    def test_certificate_timeout_does_not_repeat_request(self):
        from unittest.mock import Mock
        self.prepare();adapter=ExistingNPM(self.manager,self.data);opener=Mock();opener.open.side_effect=TimeoutError()
        with patch('managed_proxy.urllib.request.build_opener',return_value=opener):
            with self.assertRaisesRegex(NPMAPIError,'超时'):adapter._npm_request('nginx/certificates',{'provider':'letsencrypt','meta':{'dns_challenge':False}},'private-token')
        self.assertEqual(opener.open.call_count,1)

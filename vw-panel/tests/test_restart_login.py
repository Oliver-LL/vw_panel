import http.client
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'web'))
spec=importlib.util.spec_from_file_location('restart_panel',ROOT/'web/server.py');panel=importlib.util.module_from_spec(spec);spec.loader.exec_module(panel)

class RestartLoginTests(unittest.TestCase):
    def test_cookie_and_csrf_survive_server_restart_and_logout_stays_revoked(self):
        with tempfile.TemporaryDirectory() as directory:
            state=Path(directory)
            panel.atomic_json(state/'panel.json',{'host':'127.0.0.1','port':8787,'origins':[]})
            panel.atomic_json(state/'credentials.json',{'salt':'fixture','hash':'fixture-hash'})
            def start():
                app=panel.Application(state);server=panel.ThreadingHTTPServer(('127.0.0.1',0),panel.Handler)
                app.config['port']=server.server_port;server.app=app
                thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                return server,thread
            def request(server,method,path,data=None,cookie=None,csrf=None):
                connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
                headers={'Origin':'http://127.0.0.1:%s'%server.server_port,'Content-Type':'application/json'}
                if cookie:headers['Cookie']=cookie
                if csrf:headers['X-CSRF-Token']=csrf
                connection.request(method,path,json.dumps(data) if data is not None else None,headers)
                response=connection.getresponse();status=response.status;body=json.loads(response.read());cookies=response.getheader('Set-Cookie');connection.close()
                return status,body,cookies
            server,thread=start()
            try:
                with patch.object(panel,'digest',return_value='fixture-hash'):
                    status,body,cookie=request(server,'POST','/api/login',{'password':'fixture-password'})
                self.assertEqual(status,200);csrf=body['csrf'];cookie=cookie.split(';')[0]
            finally:server.shutdown();server.server_close();thread.join()
            server,thread=start()
            try:
                status,body,_=request(server,'GET','/api/session',cookie=cookie)
                self.assertEqual(status,200);self.assertEqual(body['csrf'],csrf)
                self.assertEqual(request(server,'POST','/api/logout',{},cookie=cookie)[0],403)
                self.assertEqual(request(server,'POST','/api/logout',{},cookie=cookie,csrf=csrf)[0],200)
            finally:server.shutdown();server.server_close();thread.join()
            self.assertIsNone(panel.Application(state).auth(cookie))

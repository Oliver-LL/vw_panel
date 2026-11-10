import io
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch,MagicMock
import test_manager
import browser_oauth as oauth

class Input(io.BytesIO):
    def close(self):self.payload=self.getvalue();super().close()

class Process:
    def __init__(self,args,stdin,stdout,stderr,start_new_session):
        self.args=args;self.stdin=Input();self.output=stdout;self.returncode=None
        stdout.write(b'Go to http://127.0.0.1:53682/auth?state=privateState\n');stdout.flush()
    def poll(self):return self.returncode
    def wait(self,timeout=None):
        if self.returncode is None:
            self.output.write(b'Paste the following into your remote machine --->\n{"access_token":"secret-token","refresh_token":"secret-refresh"}\n<---End paste\n');self.output.flush();self.returncode=0
        return self.returncode
    def terminate(self):self.returncode=-15
    def kill(self):self.returncode=-9

class BrowserOAuthTests(unittest.TestCase):
    setUp=test_manager.ManagerTests.setUp
    tearDown=test_manager.ManagerTests.tearDown
    def prepare(self):
        folder=self.root/'wizard';folder.mkdir()
        self.manager.remote_wizard={'id':'wizard-id','folder':str(folder),'name':'Google','type':'drive','state':'await-token','created':time.time(),'oauth_args':['drive','sensitive-config-blob']}
        self.manager.logfile=tempfile.TemporaryFile()

    def test_guided_questions_and_private_authorize_parameters(self):
        self.manager.save('core',self.data)
        self.manager.save('backup',dict(CRON='0 3 * * *',BACKUP_KEEP_DAYS=30,TIMEZONE='UTC',ZIP_PASSWORD='BackupPassword!123'))
        self.manager.logfile=tempfile.TemporaryFile()
        calls=[]
        def rc(path,data,folder):
            calls.append((path,data))
            if path=='config/create':return {'State':'client','Option':{'Name':'client_id','Required':False}}
            return {'State':'token','Option':{'Name':'config_token','Help':'rclone authorize "drive" "private-encoded-client-settings"\nThen paste the result.'}}
        with patch.object(self.manager,'rc',side_effect=rc):
            self.manager.remote_question('remote-wizard',{'name':'Drive','type':'drive','guided':True})
        result=self.manager.job['result']
        self.assertEqual(result['oauth_available']['provider'],'Google Drive')
        self.assertNotIn('private-encoded-client-settings',json.dumps(result))
        self.assertEqual(self.manager.remote_wizard['oauth_args'],['drive','private-encoded-client-settings'])
        self.assertFalse(calls[0][1]['opt']['all'])
        self.assertEqual(calls[1][1]['opt']['result'],'')

    def test_authorize_command_parser(self):
        self.assertEqual(oauth.arguments('run:\n rclone authorize "drive" "blob"\nThen paste','drive'),['drive','blob'])
        self.assertIsNone(oauth.arguments('rclone authorize "drive"; echo leak','drive'))
        self.assertIsNone(oauth.arguments('rclone authorize "unknown"','unknown'))
        self.assertIsNone(oauth.arguments('rclone authorize "dropbox"','drive'))

    def start(self,url='https://accounts.google.com/o/oauth2/auth?state=privateState'):
        self.prepare()
        with patch('browser_oauth.socket.socket',return_value=MagicMock()),patch('browser_oauth.subprocess.Popen',side_effect=Process),patch('browser_oauth.request_local',return_value=(307,{'Location':url})):
            oauth.start(self.manager,'wizard-id')

    def test_browser_link_to_callback_without_public_tokens(self):
        self.start();active=self.manager.remote_oauth;process=active['process']
        self.assertNotIn('sensitive-config-blob',str(process.args));self.assertIn(b'sensitive-config-blob',process.stdin.payload)
        public=json.dumps(self.manager.job);self.assertNotIn('sensitive-config-blob',public);self.assertNotIn('secret-token',public)
        self.assertEqual(self.manager.job['result']['browser_oauth']['provider'],'Google Drive')
        seen=[]
        def continuation(action,data):
            seen.append((action,data));self.manager.job['result']={'done':True}
        with patch('browser_oauth.request_local',return_value=(200,{})),patch('browser_oauth.subprocess.run'),patch.object(self.manager,'remote_question',side_effect=continuation):
            oauth.complete(self.manager,'wizard-id','http://127.0.0.1:53682/?code=privateCode&state=privateState')
        self.assertEqual(json.loads(seen[0][1]['answer'])['access_token'],'secret-token')
        self.assertEqual(seen[0][0],'remote-continue');self.assertIsNone(self.manager.remote_oauth)
        self.assertFalse(Path(active['path']).exists());self.assertNotIn('secret-token',json.dumps(self.manager.job))
        self.manager.logfile.seek(0);logs=self.manager.logfile.read().decode();self.assertNotIn('secret-token',logs);self.assertNotIn('privateCode',logs)

    def test_callback_rejects_other_hosts_state_duplicates_and_missing_code(self):
        self.start()
        bad=['https://attacker.example/?state=privateState&code=x','http://127.0.0.1:9999/?state=privateState&code=x','http://127.0.0.1:53682/path?state=privateState&code=x','http://127.0.0.1:53682/?state=wrong&code=x','http://127.0.0.1:53682/?state=privateState&state=privateState&code=x','http://127.0.0.1:53682/?state=privateState']
        with patch('browser_oauth.request_local') as request:
            for url in bad:
                with self.subTest(url=url),self.assertRaises(ValueError):oauth.complete(self.manager,'wizard-id',url)
            request.assert_not_called()
        with patch('browser_oauth.subprocess.run'):oauth.cancel(self.manager)

    def test_untrusted_authorization_link_cleans_up(self):
        with patch('browser_oauth.subprocess.run'),self.assertRaises(ValueError):self.start('https://attacker.example/?state=privateState')
        self.assertIsNone(self.manager.remote_oauth)
        self.assertFalse((self.root/'wizard/oauth-private.log').exists())

    def test_expired_and_denied_flows_do_not_commit(self):
        self.start();self.manager.remote_oauth['expires']=time.time()-1
        with patch('browser_oauth.subprocess.run'),self.assertRaisesRegex(ValueError,'过期'):oauth.complete(self.manager,'wizard-id','http://127.0.0.1:53682/?state=privateState&code=x')
        self.assertIsNone(self.manager.remote_oauth)

if __name__=='__main__':unittest.main()

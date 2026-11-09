import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'web'))
from session_store import SessionStore

class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        (self.root/'credentials.json').write_text(json.dumps({'salt':'first-salt','hash':'first-hash'}))
    def tearDown(self):self.temp.cleanup()
    def test_restart_preserves_cookie_csrf_and_original_expiry_without_storing_raw_token(self):
        store=SessionStore(self.root);token,csrf=store.create()
        raw=(self.root/'sessions.json').read_text();self.assertNotIn(token,raw)
        self.assertEqual((self.root/'sessions.json').stat().st_mode&0o777,0o600)
        restarted=SessionStore(self.root);self.assertEqual(restarted.auth(token),csrf)
        with patch('session_store.time.time',return_value=time.time()+43201):self.assertIsNone(SessionStore(self.root).auth(token))
    def test_logout_survives_restart_and_does_not_revoke_other_session(self):
        store=SessionStore(self.root);first,_=store.create();second,csrf=store.create()
        SessionStore(self.root).revoke(first)
        restarted=SessionStore(self.root);self.assertIsNone(restarted.auth(first));self.assertEqual(restarted.auth(second),csrf)
    def test_password_reset_invalidates_live_and_persisted_sessions(self):
        store=SessionStore(self.root);token,_=store.create()
        (self.root/'credentials.json').write_text(json.dumps({'salt':'second-salt','hash':'second-hash'}))
        self.assertIsNone(store.auth(token));self.assertIsNone(SessionStore(self.root).auth(token))
        token,csrf=store.create();self.assertEqual(SessionStore(self.root).auth(token),csrf)
    def test_bounded_sessions_and_corrupt_file_fail_closed(self):
        store=SessionStore(self.root,maximum=2);first,_=store.create();store.create();last,csrf=store.create()
        self.assertIsNone(SessionStore(self.root).auth(first));self.assertEqual(SessionStore(self.root).auth(last),csrf)
        (self.root/'sessions.json').write_text('not-json');self.assertIsNone(SessionStore(self.root).auth(last))
    def test_future_or_oversized_records_are_not_accepted(self):
        store=SessionStore(self.root);token,csrf=store.create()
        value=json.loads(store.path.read_text());value['sessions'][store.key(token)][1]=time.time()+3600
        store.path.write_text(json.dumps(value));self.assertIsNone(SessionStore(self.root).auth(token))
        store.path.write_text('x'*65537);self.assertEqual(SessionStore(self.root).sessions,{})
    def test_session_storage_failure_does_not_authenticate_new_cookie(self):
        store=SessionStore(self.root)
        with patch.object(store,'persist',side_effect=ValueError('disk error')),self.assertRaises(ValueError):store.create()
        self.assertEqual(store.sessions,{})

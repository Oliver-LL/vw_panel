import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import test_manager as fixtures

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'web'))
from manager import Manager, update_vars

class ApplicationStateTests(unittest.TestCase):
    setUp=fixtures.ManagerTests.setUp
    tearDown=fixtures.ManagerTests.tearDown
    configured=fixtures.ManagerTests.configured
    wait_job=fixtures.ManagerTests.wait_job

    def test_saved_applied_modified_and_reverted_configuration(self):
        self.assertEqual(self.manager.application_state()['core'],'unconfigured')
        env=self.configured()
        self.assertEqual(self.manager.application_state()['core'],'pending')
        self.manager.note_applied('core')
        old=env.read_bytes()
        self.assertEqual(self.manager.application_state()['core'],'applied')
        self.manager.save('advanced',{'TZ':'UTC','ADMIN_SESSION_LIFETIME':30})
        self.assertEqual(self.manager.application_state()['core'],'pending')
        # The current revision, not a permanently set boolean, determines pending state.
        env.write_bytes(old)
        self.assertEqual(self.manager.application_state()['core'],'applied')
        self.assertEqual(Manager(self.manager.state,self.manager.memory).application_state()['core'],'applied')

    def test_failed_application_keeps_pending_successful_application_clears_it(self):
        env=self.configured();update_vars(env,{'ADMIN_TOKEN':'$argon2id$private'})
        with patch.object(self.manager,'_command',side_effect=ValueError('failed image pull')):
            self.manager.start('deploy',{})
            self.assertEqual(self.wait_job()['state'],'failed')
        self.assertEqual(self.manager.application_state()['core'],'pending')
        def command(args,**kw):return 'true' if '{{.State.Running}}' in args else ''
        with patch.object(self.manager,'_command',side_effect=command),patch('manager.subprocess.run',return_value=SimpleNamespace(returncode=0)):
            self.manager.start('deploy',{})
            self.assertEqual(self.wait_job()['state'],'success')
        self.assertEqual(self.manager.application_state()['core'],'applied')
        self.assertEqual(Manager(self.manager.state,self.manager.memory).application_state()['core'],'applied')
        self.assertNotIn('private',json.dumps(self.manager.config()))
        self.assertNotIn(self.manager.revision('core'),json.dumps(self.manager.config()))

    def test_backup_and_core_track_independently(self):
        self.configured();self.manager.note_applied('core')
        self.manager.save('backup',{'CRON':'0 3 * * *','TIMEZONE':'Asia/Shanghai','BACKUP_KEEP_DAYS':30,'ZIP_PASSWORD':'SecretBackupPassword!'})
        self.assertEqual(self.manager.application_state(),{'core':'applied','backup':'pending'})
        self.manager.note_applied('backup')
        with patch.object(self.manager,'_command',return_value='$argon2id$v=19$m=65536,t=3,p=4$salt$hash'):
            self.manager.start('admin-token',{'password':'NewAdminSecretPassword!'})
            self.assertEqual(self.wait_job()['state'],'success')
        self.assertEqual(self.manager.application_state(),{'core':'pending','backup':'applied'})

    def test_existing_deployment_without_record_is_unknown_and_noop_save_is_not_pending(self):
        self.configured();(self.manager.state/'application.json').unlink()
        self.assertEqual(self.manager.application_state()['core'],'unknown')
        self.manager.save('core',self.data)
        self.assertEqual(self.manager.application_state()['core'],'unknown')
        self.manager.save('core',dict(self.data,SERVER_ADMIN_EMAIL='new@example.com'))
        self.assertEqual(self.manager.application_state()['core'],'pending')

class DockerStatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec=importlib.util.spec_from_file_location('presentation_panel',ROOT/'web/server.py')
        cls.panel=importlib.util.module_from_spec(spec);spec.loader.exec_module(cls.panel)

    def test_absent_docker_differs_from_engine_unavailable(self):
        with patch.object(self.panel.subprocess,'run',side_effect=FileNotFoundError):
            self.assertFalse(self.panel.status()['docker_installed'])
        with patch.object(self.panel.subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout='')):
            result=self.panel.status();self.assertTrue(result['docker_installed']);self.assertFalse(result['available'])

if __name__=='__main__':unittest.main()

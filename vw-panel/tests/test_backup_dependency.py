import unittest
from unittest.mock import patch
from types import SimpleNamespace
from test_presentation_state import ApplicationStateTests
from manager import update_vars

class BackupDependencyTests(unittest.TestCase):
    setUp=ApplicationStateTests.setUp
    tearDown=ApplicationStateTests.tearDown
    configured=ApplicationStateTests.configured
    wait_job=ApplicationStateTests.wait_job

    def prepare(self):
        env=self.configured()
        update_vars(env,{'ADMIN_TOKEN':'$argon2id$private'})
        self.manager.save('backup',{'CRON':'0 3 * * *','TIMEZONE':'Asia/Shanghai','BACKUP_KEEP_DAYS':30,'ZIP_PASSWORD':'SecretBackupPassword!'})

    def test_stopped_core_starts_before_backup(self):
        self.prepare();calls=[];core_checks=0
        def command(args,**kwargs):
            nonlocal core_checks
            calls.append(args)
            if '{{.State.Running}}' in args:
                if args[-1]=='vaultwarden':
                    core_checks+=1
                    return 'false' if core_checks==1 else 'true'
                return 'true'
            return ''
        with patch.object(self.manager,'_command',side_effect=command),patch('manager.subprocess.run',return_value=SimpleNamespace(returncode=0)):
            self.manager.start('backup-start',{})
            self.assertEqual(self.wait_job()['state'],'success')
        ups=[c for c in calls if 'up' in c]
        self.assertEqual(len(ups),2)
        self.assertIn('/vaultwarden/',ups[0][ups[0].index('-f')+1])
        self.assertIn('/vaultwarden-backup/',ups[1][ups[1].index('-f')+1])

    def test_core_start_failure_prevents_backup_start(self):
        self.prepare();calls=[]
        def command(args,**kwargs):
            calls.append(args)
            if '{{.State.Running}}' in args:return 'false'
            if 'up' in args:raise ValueError('core start failed')
            return ''
        with patch.object(self.manager,'_command',side_effect=command),patch('manager.subprocess.run',return_value=SimpleNamespace(returncode=0)):
            self.manager.start('backup-start',{})
            self.assertEqual(self.wait_job()['state'],'failed')
        self.assertFalse(any('up' in c and '--force-recreate' in c for c in calls))

    def test_running_core_is_not_redeployed(self):
        self.prepare()
        with patch.object(self.manager,'_command',return_value='true'),patch.object(self.manager,'_deploy_core') as deploy:
            self.manager.start('backup-start',{})
            self.assertEqual(self.wait_job()['state'],'success')
            deploy.assert_not_called()

    def test_core_upgrade_does_not_restart_backup(self):
        self.prepare();calls=[]
        def command(args,**kwargs):
            calls.append(args);return 'true' if '{{.State.Running}}' in args else ''
        with patch.object(self.manager,'_command',side_effect=command),patch('manager.subprocess.run',return_value=SimpleNamespace(returncode=0)):
            self.manager.start('upgrade',{'confirmed':True})
            self.assertEqual(self.wait_job()['state'],'success')
        self.assertFalse(any('/vaultwarden-backup/' in ' '.join(c) for c in calls))

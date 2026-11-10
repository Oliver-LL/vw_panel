import json
import unittest
from unittest.mock import patch
import test_operations as fixtures
from manager import read_vars
from backup_targets import read,save,SENTINEL

class TargetTests(unittest.TestCase):
    setUp=fixtures.OperationsTests.setUp
    tearDown=fixtures.OperationsTests.tearDown
    configured=fixtures.OperationsTests.configured
    backup=fixtures.OperationsTests.backup
    wait_job=fixtures.OperationsTests.wait_job

    def prepare(self):
        self.backup();self.folder=self.manager.paths()[1];self.env=self.folder/'backup.env';self.meta=self.folder/'panel-targets.json'
        self.remote=self.folder/'vw-backup-config/rclone/rclone.conf';self.remote.write_text(self.remote.read_text()+'\n[Cloud]\ntype = webdav\n')

    def test_disabled_config_is_retained_and_only_active_targets_reach_env(self):
        self.prepare();save(self.manager,[{'name':'BitwardenBackup','directory':'/bitwarden/backup/archive','enabled':True},{'name':'Cloud','directory':'/private-backups','enabled':False}])
        targets=read(self.manager);self.assertEqual(len(targets),2);self.assertFalse(targets[1]['enabled']);self.assertTrue(targets[0]['enabled'])
        env=read_vars(self.env);self.assertEqual(env['RCLONE_REMOTE_NAME'],'BitwardenBackup');self.assertNotIn('RCLONE_REMOTE_NAME_1',env)
        self.assertEqual(self.manager.config()['extras']['targets'],targets)
        self.assertEqual(self.meta.stat().st_mode&0o777,0o600)

    def test_toggle_reloads_running_backup_and_rolls_back_failure(self):
        self.prepare();save(self.manager,[{'name':'BitwardenBackup','directory':'/local','enabled':True},{'name':'Cloud','directory':'/cloud','enabled':True}]);targets=read(self.manager)
        with patch('backup_targets.running',return_value=True),patch.object(self.manager,'_command',return_value='') as command:
            self.manager.start('backup-target-toggle',{'id':targets[0]['id'],'enabled':False});self.assertEqual(self.wait_job()['state'],'success')
        self.assertIn('--force-recreate',command.call_args[0][0]);self.assertEqual(read_vars(self.env)['RCLONE_REMOTE_NAME'],'Cloud');self.assertFalse(read(self.manager)[0]['enabled'])
        before=self.env.read_text(),self.meta.read_text()
        with patch('backup_targets.running',return_value=True),patch.object(self.manager,'_command',side_effect=ValueError('reload failed')):
            self.manager.start('backup-target-toggle',{'id':targets[0]['id'],'enabled':True});self.assertEqual(self.wait_job()['state'],'failed')
        self.assertEqual((self.env.read_text(),self.meta.read_text()),before)

    def test_disabling_last_target_stops_and_reenabling_resumes_only_automatic_pause(self):
        self.prepare();target=read(self.manager)[0]
        with patch('backup_targets.running',side_effect=[True,False]),patch.object(self.manager,'_command',return_value='') as command:
            self.manager.start('backup-target-toggle',{'id':target['id'],'enabled':False});self.assertEqual(self.wait_job()['state'],'success')
        self.assertIn('stop',command.call_args[0][0]);self.assertEqual(read_vars(self.env)['RCLONE_REMOTE_NAME'],SENTINEL);self.assertFalse(read(self.manager)[0]['enabled']);self.assertTrue(json.loads(self.meta.read_text())['paused_for_targets']);self.assertIn('BitwardenBackup',self.env.with_name('backup.env.panel-backup').read_text())
        with patch.object(self.manager,'_command') as command:
            self.manager.start('backup-now',{});self.assertEqual(self.wait_job()['state'],'failed');command.assert_not_called()
        with patch('backup_targets.running',side_effect=[False,True,True]),patch.object(self.manager,'_command',return_value=''):
            self.manager.start('backup-target-toggle',{'id':target['id'],'enabled':True});self.assertEqual(self.wait_job()['state'],'success')
        self.assertEqual(read_vars(self.env)['RCLONE_REMOTE_NAME'],'BitwardenBackup');self.assertNotIn('VW_PANEL_TARGETS_DISABLED',read_vars(self.env))
        self.assertFalse(json.loads(self.meta.read_text())['paused_for_targets'])
        with patch('backup_targets.running',return_value=False),patch.object(self.manager,'_command') as command:
            self.manager.start('backup-target-toggle',{'id':target['id'],'enabled':False});self.assertEqual(self.wait_job()['state'],'success');command.assert_not_called()
            self.manager.start('backup-target-toggle',{'id':target['id'],'enabled':True});self.assertEqual(self.wait_job()['state'],'success');command.assert_not_called()

    def test_original_bash_edits_are_imported_while_disabled_definitions_survive(self):
        from manager import update_vars
        self.prepare();save(self.manager,[{'name':'BitwardenBackup','directory':'/local','enabled':False},{'name':'Cloud','directory':'/cloud','enabled':True}])
        update_vars(self.env,{'RCLONE_REMOTE_DIR':'/changed-by-bash'})
        targets=read(self.manager);self.assertEqual(targets[0]['name'],'Cloud');self.assertEqual(targets[0]['directory'],'/changed-by-bash');self.assertTrue(targets[0]['enabled'])
        self.assertEqual(targets[1]['name'],'BitwardenBackup');self.assertFalse(targets[1]['enabled'])
        save(self.manager,targets);self.assertEqual(read_vars(self.env)['RCLONE_REMOTE_DIR'],'/changed-by-bash')

    def test_invalid_targets_or_unknown_state_never_change_env(self):
        self.prepare();before=self.env.read_text()
        for targets in [[{'name':'Cloud','directory':'../bad','enabled':True}],[{'name':'Missing','directory':'/x','enabled':True}],[{'name':'Cloud','directory':'/x','enabled':'false'}]]:
            with self.assertRaises(ValueError):save(self.manager,targets)
            self.assertEqual(self.env.read_text(),before)
        with patch('backup_targets.running',side_effect=ValueError('unknown state')):
            self.manager.start('backup-target-toggle',{'id':read(self.manager)[0]['id'],'enabled':False});self.assertEqual(self.wait_job()['state'],'failed')
        self.assertEqual(self.env.read_text(),before)

if __name__=='__main__':unittest.main()

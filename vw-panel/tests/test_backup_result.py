import json
from pathlib import Path
from unittest.mock import patch
import unittest
import test_operations as fixtures
from manager import read_vars,update_vars
from backup_result import prepare_storage,run_now
from backup_targets import save,read

class BackupResultTests(unittest.TestCase):
    setUp=fixtures.OperationsTests.setUp
    tearDown=fixtures.OperationsTests.tearDown
    configured=fixtures.OperationsTests.configured
    backup=fixtures.OperationsTests.backup
    wait_job=fixtures.OperationsTests.wait_job

    def prepare(self):
        self.backup();self.folder=self.manager.paths()[1]
        self.config=self.folder/'vw-backup-config/rclone/rclone.conf'
        self.config.write_text(self.config.read_text()+'\n[MyCloud]\ntype = s3\nsecret_access_key = HiddenFixtureKey\n')

    def test_existing_mount_migration_preserves_files_and_disabled_targets(self):
        self.prepare();compose=self.folder/'docker-compose.yml'
        compose.write_text(compose.read_text().replace('/vwp/backups','/bitwarden/backup'))
        update_vars(self.folder/'backup.env',{'RCLONE_REMOTE_NAME':'BitwardenBackup','RCLONE_REMOTE_DIR':'/bitwarden/backup/archive'})
        (self.folder/'panel-targets.json').write_text(json.dumps({'targets':[{'id':'enabled','name':'BitwardenBackup','directory':'/bitwarden/backup/archive','enabled':True},{'id':'disabled','name':'BitwardenBackup','directory':'/bitwarden/backup/other','enabled':False}], 'active':[{'name':'BitwardenBackup','directory':'/bitwarden/backup/archive'}],'paused_for_targets':True}))
        retained=self.folder/'backups/uploaded/old.zip';retained.parent.mkdir();retained.write_bytes(b'keep')
        prepare_storage(self.manager)
        self.assertIn('./backups:/vwp/backups',compose.read_text());self.assertNotIn(':/bitwarden/backup',compose.read_text())
        self.assertEqual(read_vars(self.folder/'backup.env')['RCLONE_REMOTE_DIR'],'/vwp/backups/archive')
        targets=read(self.manager);self.assertEqual([row['id'] for row in targets],['enabled','disabled']);self.assertEqual(targets[1]['directory'],'/vwp/backups/other');self.assertFalse(targets[1]['enabled']);self.assertEqual(retained.read_bytes(),b'keep')
        self.assertTrue(json.loads((self.folder/'panel-targets.json').read_text())['paused_for_targets'])
        first=compose.read_text();prepare_storage(self.manager);self.assertEqual(compose.read_text(),first)

    def test_manual_backup_reports_runtime_target_instead_of_unapplied_saved_target(self):
        self.prepare();runtime={'env':['RCLONE_REMOTE_NAME=MyCloud','RCLONE_REMOTE_DIR=/bucket/actual','ZIP_PASSWORD=NotPublic'], 'mounts':[{'Destination':'/vwp/backups'}]}
        def command(args,*unused,**kwargs):
            if args[1]=='inspect':self.assertTrue(kwargs.get('secret'));return json.dumps(runtime)
            self.assertEqual(args,['docker','exec','vaultwarden-backup','/app/backup.sh']);return ''
        with patch.object(self.manager,'_command',side_effect=command):
            self.manager.start('backup-now',{});job=self.wait_job();self.assertEqual(job['state'],'success')
        result=job['result']['backup_result'];self.assertEqual(result['destinations'],[{'name':'MyCloud','directory':'/bucket/actual','local':False}]);self.assertEqual(result['local_files'],[])
        self.assertNotIn('NotPublic',json.dumps(job));self.assertNotIn('HiddenFixtureKey',json.dumps(job))

    def test_local_output_appears_and_existing_import_survives(self):
        self.prepare();kept=self.folder/'backups/uploaded.zip';kept.write_bytes(b'existing')
        runtime={'env':['RCLONE_REMOTE_NAME=BitwardenBackup','RCLONE_REMOTE_DIR=/vwp/backups/archive'], 'mounts':[{'Destination':'/vwp/backups'}]}
        def command(args,*unused,**kwargs):
            if args[1]=='inspect':return json.dumps(runtime)
            output=self.folder/'backups/archive/new.zip';output.parent.mkdir();output.write_bytes(b'new');return ''
        with patch.object(self.manager,'_command',side_effect=command):
            self.manager.start('backup-now',{});job=self.wait_job();self.assertEqual(job['state'],'success')
        self.assertEqual(job['result']['backup_result']['local_files'],['archive/new.zip']);self.assertEqual(kept.read_bytes(),b'existing')

    def test_dangerous_runtime_mount_and_failed_backup_never_report_success(self):
        self.prepare()
        for destination in ['/bitwarden/backup','/bitwarden/backup/archive']:
            runtime={'env':['RCLONE_REMOTE_NAME=MyCloud','RCLONE_REMOTE_DIR=/bucket/path'],'mounts':[{'Destination':destination}]}
            with patch.object(self.manager,'_command',return_value=json.dumps(runtime)) as command:
                self.manager.start('backup-now',{});job=self.wait_job();self.assertEqual(job['state'],'failed');self.assertIn('重启并应用',job['error']);self.assertEqual(command.call_count,1)
        runtime={'env':['RCLONE_REMOTE_NAME=MyCloud','RCLONE_REMOTE_DIR=/bucket/path'],'mounts':[{'Destination':'/vwp/backups'}]}
        with patch.object(self.manager,'_command',side_effect=[json.dumps(runtime),ValueError('upload failed')]):
            self.manager.start('backup-now',{});job=self.wait_job();self.assertEqual(job['state'],'failed');self.assertFalse(job.get('result',{}).get('backup_result'))

    def test_custom_long_mount_is_not_silently_rewritten(self):
        self.prepare();compose=self.folder/'docker-compose.yml';original='services:\n  backup:\n    volumes:\n      - type: bind\n        source: ./backups\n        target: /bitwarden/backup\n'
        compose.write_text(original)
        with self.assertRaisesRegex(ValueError,'自定义'):prepare_storage(self.manager)
        self.assertEqual(compose.read_text(),original)

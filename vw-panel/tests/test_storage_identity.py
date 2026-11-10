import configparser
import json
from unittest.mock import patch
import unittest
import test_operations as fixtures
from backup_targets import read,save
from manager import read_vars

class StorageIdentityTests(unittest.TestCase):
    setUp=fixtures.OperationsTests.setUp
    tearDown=fixtures.OperationsTests.tearDown
    configured=fixtures.OperationsTests.configured
    backup=fixtures.OperationsTests.backup
    wait_job=fixtures.OperationsTests.wait_job

    def prepare(self):
        self.backup();self.folder=self.manager.paths()[1];self.file=self.folder/'vw-backup-config/rclone/rclone.conf'
        self.file.write_text(self.file.read_text()+'\n[MyCloud]\ntype = s3\nprovider = Cloudflare\nendpoint = https://'+('a'*32)+'.r2.cloudflarestorage.com\naccess_key_id = OriginalAccess1234\nsecret_access_key = OriginalSecretFixture\nupload_cutoff = 100M\n\n[Other]\ntype = s3\nprovider = Cloudflare\nsecret_access_key = OtherSecretFixture\n')
        save(self.manager,[{'name':'BitwardenBackup','directory':'/vwp/backups/archive','enabled':True},{'name':'MyCloud','directory':'bucket/VaultBackup','enabled':True},{'name':'MyCloud','directory':'bucket/old','enabled':False}])
        self.manager.remember_preset_target('MyCloud','bucket/VaultBackup')
        self.data={'preset':'r2','name':'CloudFlareR2','original_name':'MyCloud','account_id':'','access_key_id':'','secret_access_key':'','bucket':'bucket','path':'VaultBackup','no_check_bucket':True,'set_target':False}

    def parse(self):
        parser=configparser.ConfigParser(interpolation=None);parser.read(self.file);return parser

    def test_rename_reuses_credentials_and_preserves_all_target_identities_and_flags(self):
        self.prepare();before=read(self.manager)
        with patch.object(self.manager,'rclone_probe',return_value='') as probe:
            self.manager.start('remote-preset-save',self.data);job=self.wait_job();self.assertEqual(job['state'],'success')
        parsed=self.parse();self.assertNotIn('MyCloud',parsed)
        self.assertEqual(parsed['CloudFlareR2']['access_key_id'],'OriginalAccess1234');self.assertEqual(parsed['CloudFlareR2']['secret_access_key'],'OriginalSecretFixture')
        self.assertEqual(parsed['CloudFlareR2']['upload_cutoff'],'100M');self.assertEqual(parsed['Other']['secret_access_key'],'OtherSecretFixture')
        after=read(self.manager);self.assertEqual([item['id'] for item in after],[item['id'] for item in before]);self.assertEqual([item['enabled'] for item in after],[item['enabled'] for item in before])
        self.assertEqual([item['name'] for item in after],['BitwardenBackup','CloudFlareR2','CloudFlareR2'])
        self.assertEqual(read_vars(self.folder/'backup.env')['RCLONE_REMOTE_NAME_1'],'CloudFlareR2')
        self.assertEqual(self.manager.preset_targets(),{'CloudFlareR2':'/bucket/VaultBackup'})
        self.assertTrue(probe.call_args.args[0][1].startswith('CloudFlareR2:'))
        self.assertNotIn('OriginalSecretFixture',json.dumps(job));self.assertNotIn('OriginalAccess1234',json.dumps(job))

    def test_unchanged_name_and_explicit_secret_replacement(self):
        self.prepare()
        with patch.object(self.manager,'rclone_probe',return_value=''):
            self.manager.start('remote-preset-save',dict(self.data,name='MyCloud',secret_access_key='ReplacementSecretFixture'))
            self.assertEqual(self.wait_job()['state'],'success')
        parsed=self.parse();self.assertEqual(parsed['MyCloud']['access_key_id'],'OriginalAccess1234');self.assertEqual(parsed['MyCloud']['secret_access_key'],'ReplacementSecretFixture')

    def test_collision_missing_source_and_invalid_source_do_not_change_config(self):
        self.prepare();original=self.file.read_text()
        for changes in ({'name':'Other'},{'original_name':'Missing'},{'original_name':'../MyCloud'}):
            with patch.object(self.manager,'rclone_probe') as probe:
                try:self.manager.start('remote-preset-save',dict(self.data,**changes))
                except ValueError:pass
                else:self.assertEqual(self.wait_job()['state'],'failed')
                probe.assert_not_called();self.assertEqual(self.file.read_text(),original)

    def test_validation_or_reload_failure_restores_name_credentials_and_targets(self):
        self.prepare();original=self.file.read_text();env=(self.folder/'backup.env').read_text();targets=read(self.manager);cache=self.manager.preset_targets()
        with patch.object(self.manager,'rclone_probe',side_effect=ValueError('verification failed')):
            self.manager.start('remote-preset-save',self.data);self.assertEqual(self.wait_job()['state'],'failed')
        self.assertEqual(self.file.read_text(),original)
        def command(args,*unused,**kwargs):
            if not hasattr(command,'failed'):command.failed=True;raise ValueError('reload failed')
            self.assertIn('MyCloud',self.parse());self.assertNotIn('CloudFlareR2',self.parse());return ''
        with patch.object(self.manager,'rclone_probe',return_value=''),patch('backup_targets.running',return_value=True),patch.object(self.manager,'_command',side_effect=command):
            self.manager.start('remote-preset-save',self.data);self.assertEqual(self.wait_job()['state'],'failed')
        self.assertEqual(self.file.read_text(),original);self.assertEqual((self.folder/'backup.env').read_text(),env);self.assertEqual(read(self.manager),targets);self.assertEqual(self.manager.preset_targets(),cache)

    def test_rename_with_save_as_target_preserves_other_disabled_directory(self):
        self.prepare();before=read(self.manager)
        with patch.object(self.manager,'rclone_probe',return_value=''),patch('backup_targets.running',return_value=True),patch.object(self.manager,'_command',return_value=''):
            self.manager.start('remote-preset-save',dict(self.data,set_target=True));job=self.wait_job();self.assertEqual(job['state'],'success')
        after=read(self.manager);self.assertEqual([item['id'] for item in before],[item['id'] for item in after]);self.assertFalse(after[-1]['enabled']);self.assertEqual(after[-1]['directory'],'bucket/old')
        self.assertTrue(job['result']['storage_saved']['target_applied'])

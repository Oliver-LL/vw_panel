import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import test_operations as fixtures
from manager import update_vars

class RemoteBrowserTests(unittest.TestCase):
    setUp=fixtures.OperationsTests.setUp
    tearDown=fixtures.OperationsTests.tearDown
    configured=fixtures.OperationsTests.configured
    wait_job=fixtures.OperationsTests.wait_job
    backup=fixtures.OperationsTests.backup

    def setup_remotes(self):
        self.backup();self.backup_dir=self.manager.paths()[1]
        self.config=self.backup_dir/'vw-backup-config/rclone/rclone.conf'
        self.config.write_text(self.config.read_text()+'\n[Cloud]\ntype = s3\nprovider = Cloudflare\nsecret_access_key = PrivateSigningSecret\n[Unknown]\ntype = s3\n')
        self.manager.remember_preset_target('Cloud','/my-bucket/VaultBackup')

    def test_correct_paths_and_no_bucket_guess(self):
        self.setup_remotes()
        self.assertEqual(self.manager.remote_directory('Cloud'),'my-bucket/VaultBackup')
        self.assertEqual(self.manager.remote_directory('BitwardenBackup'),'/bitwarden/backup/archive')
        update_vars(self.backup_dir/'backup.env',{'RCLONE_REMOTE_NAME_1':'Cloud','RCLONE_REMOTE_DIR_1':'/real-bucket/backups'})
        self.assertEqual(self.manager.remote_directory('Cloud'),'real-bucket/backups')
        with self.assertRaisesRegex(ValueError,'Bucket'):self.manager.remote_directory('Unknown')
        with self.assertRaises(ValueError):self.manager.remote_directory('Missing')
        with self.assertRaises(ValueError):self.manager.remote_directory('Cloud','/bucket/../outside')

    def test_read_download_context_and_partial_cleanup(self):
        self.setup_remotes();seen=[]
        def probe(args,config_file,**kwargs):
            seen.append((args,kwargs))
            return json.dumps([{'Path':'one.zip','Size':123,'ModTime':'today'},{'Path':'ignore.txt','Size':1}])
        with patch.object(self.manager,'rclone_probe',side_effect=probe):
            self.manager.start('remote-list',{'name':'Cloud'})
            job=self.wait_job();self.assertEqual(job['state'],'success')
            self.assertEqual(job['result']['remote_location'],{'name':'Cloud','directory':'my-bucket/VaultBackup'})
            self.assertEqual([row['name'] for row in job['result']['remote_files']],['one.zip'])
        self.assertEqual(seen[0][0][1],'Cloud:my-bucket/VaultBackup')
        def download(args,config_file,**kwargs):
            self.assertEqual(args[0],'copyto');self.assertEqual(args[1],'Cloud:my-bucket/VaultBackup/one.zip')
            (kwargs['download_dir']/Path(args[2]).name).write_bytes(b'archive')
            return ''
        with patch.object(self.manager,'rclone_probe',side_effect=download):
            self.manager.start('remote-download',{'name':'Cloud','file':'one.zip'})
            job=self.wait_job();self.assertEqual(job['state'],'success')
            self.assertTrue((self.backup_dir/'backups'/job['result']['downloaded_file']).exists())
        before=list((self.backup_dir/'backups/downloaded').iterdir())
        def failed(args,config_file,**kwargs):
            (kwargs['download_dir']/Path(args[2]).name).write_bytes(b'partial');raise ValueError('connection lost')
        with patch.object(self.manager,'rclone_probe',side_effect=failed):
            self.manager.start('remote-download',{'name':'Cloud','file':'two.zip'})
            self.assertEqual(self.wait_job()['state'],'failed')
        self.assertEqual(list((self.backup_dir/'backups/downloaded').iterdir()),before)

    def test_probe_mounts_local_files_and_shows_redacted_errors(self):
        self.setup_remotes();self.manager.logfile=tempfile.TemporaryFile();seen=[]
        def fail(args,stdout=None,stderr=None,timeout=None):
            seen.append(args);stderr.write(b'403 SignatureDoesNotMatch PrivateSigningSecret');return SimpleNamespace(returncode=1)
        with patch('operations.subprocess.run',side_effect=fail),self.assertRaisesRegex(ValueError,'SignatureDoesNotMatch'):
            self.manager.rclone_probe(['lsjson','Cloud:my-bucket/VaultBackup'],self.config,label='读取备份')
        self.assertIn(str(self.backup_dir/'backups')+':/bitwarden/backup:ro',seen[0])
        self.assertIn(str(self.manager.paths()[0]/'data')+':/bitwarden/data:ro',seen[0])
        self.manager.logfile.seek(0);output=self.manager.logfile.read().decode()
        self.assertNotIn('PrivateSigningSecret',output);self.assertIn('SignatureDoesNotMatch',output)

    def test_s3_connection_check_reads_bucket_without_mkdir(self):
        self.setup_remotes()
        with patch.object(self.manager,'rclone_probe',return_value='') as probe:
            self.manager.start('remote-check',{'name':'Cloud'})
            self.assertEqual(self.wait_job()['state'],'success')
        self.assertEqual(probe.call_args[0][0],['lsf','Cloud:my-bucket','--max-depth','1'])

if __name__=='__main__':unittest.main()

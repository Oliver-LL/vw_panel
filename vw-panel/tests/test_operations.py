import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'web'))
from manager import Manager, read_vars, update_vars
from operations import safe_relative
import test_manager as fixtures


class OperationsTests(unittest.TestCase):
    setUp=fixtures.ManagerTests.setUp
    tearDown=fixtures.ManagerTests.tearDown
    configured=fixtures.ManagerTests.configured
    wait_job=fixtures.ManagerTests.wait_job

    def backup(self):
        self.configured()
        self.manager.save('backup',dict(CRON='0 3 * * *',BACKUP_KEEP_DAYS=30,TIMEZONE='Asia/Shanghai',ZIP_PASSWORD='BackupPassword!123'))

    def test_full_core_settings_push_clear_and_custom_cron(self):
        env=self.configured()
        self.manager.save('advanced',{'WEBSOCKET_ENABLED':True,'WEB_VAULT_ENABLED':False,'EMERGENCY_ACCESS_ALLOWED':True,
            'ENABLE_DB_WAL':True,'ADMIN_RATELIMIT_MAX_BURST':8,'ADMIN_RATELIMIT_SECONDS':60,'ADMIN_SESSION_LIFETIME':30,'TZ':'UTC'})
        self.assertEqual(read_vars(env)['WEB_VAULT_ENABLED'],'false')
        self.manager.save('push',{'enabled':True,'id':'abcdefgh-1234','key':'super-secret-key'})
        self.assertNotIn('super-secret-key',json.dumps(self.manager.config()))
        self.manager.save('push',{'enabled':False})
        self.assertNotIn('PUSH_INSTALLATION_KEY',read_vars(env))
        self.manager.save('backup',dict(CRON='15 2 * * 1-5',BACKUP_KEEP_DAYS=7,TIMEZONE='UTC',ZIP_PASSWORD='BackupPassword!123'))
        for cron in ('60 0 * * *','* */0 * * *','1 2 3 4','0 0 1-40 * *'):
            with self.assertRaises(ValueError):self.manager.save('backup',dict(CRON=cron,BACKUP_KEEP_DAYS=7,TIMEZONE='UTC'))

    def test_notification_secrets_and_multiple_targets(self):
        self.backup()
        password="Secret'$(printf should-not-run)!"
        data={'MAIL_SMTP_ENABLE':True,'MAIL_TO':'to@example.com','from':'from@example.com','host':'smtp.example.com',
            'port':587,'security':'starttls','user':'account','password':password,'MAIL_WHEN_SUCCESS':True,
            'MAIL_WHEN_FAILURE':True,'MAIL_FORCE_THREAD':False}
        self.manager.save('notifications',data)
        env=read_vars(self.manager.paths()[1]/'backup.env')
        self.assertNotIn(password,json.dumps(self.manager.config()))
        self.assertEqual(self.manager.config()['extras']['notification_smtp']['user'],'account')
        self.manager.save('notifications',dict(data,password=''))
        self.assertEqual(env['MAIL_SMTP_VARIABLES'],read_vars(self.manager.paths()[1]/'backup.env')['MAIL_SMTP_VARIABLES'])
        config=self.manager.paths()[1]/'vw-backup-config/rclone/rclone.conf'
        config.write_text(config.read_text()+'\n[CloudTwo]\ntype = webdav\n')
        self.manager.save('targets',{'targets':[{'name':'BitwardenBackup','directory':'/local'},{'name':'CloudTwo','directory':'/remote'}]})
        env=read_vars(self.manager.paths()[1]/'backup.env')
        self.assertEqual(env['RCLONE_REMOTE_NAME_1'],'CloudTwo')
        self.manager.save('notifications',{'MAIL_SMTP_ENABLE':False})
        self.assertNotIn('MAIL_SMTP_VARIABLES',read_vars(self.manager.paths()[1]/'backup.env'))

    def test_remote_questions_keep_existing_config_until_complete(self):
        self.backup()
        config=self.manager.paths()[1]/'vw-backup-config/rclone/rclone.conf'
        original=config.read_text()
        def rc(path,data,folder):
            if path=='config/create':
                return {'State':'next-step','Option':{'Name':'client_secret','Default':'do-not-expose','IsPassword':True,'Help':'Enter secret'}}
            file=Path(folder)/'rclone/rclone.conf'
            file.write_text(file.read_text()+'\n[TestCloud]\ntype = webdav\n')
            return {'State':''}
        with patch.object(self.manager,'rc',side_effect=rc):
            self.manager.start('remote-wizard',{'name':'TestCloud','type':'webdav'})
            job=self.wait_job();self.assertEqual(job['state'],'success')
            self.assertNotIn('do-not-expose',json.dumps(job))
            self.assertEqual(config.read_text(),original)
            self.manager.start('remote-continue',{'wizard_id':job['result']['wizard_id'],'answer':'hidden secret'})
            self.assertEqual(self.wait_job()['state'],'success')
        self.assertIn('TestCloud',config.read_text())
        self.assertIn('BitwardenBackup',config.read_text())

    def test_restore_safely_rolls_back_if_new_container_fails(self):
        self.backup()
        app,backup=self.manager.paths()
        (app/'data/original').write_text('original data')
        archive=backup/'backups/backup.zip';archive.write_bytes(b'fixture')
        commands=[];inspects=0
        def command(args,input_text=None,secret=False):
            nonlocal inspects
            commands.append(args)
            output=next((v[:-len(':/output')] for v in args if v.endswith(':/output')),None)
            if output:
                with sqlite3.connect(str(Path(output)/'db.20261107.sqlite3')) as conn:conn.execute('create table fixture (id integer)')
            if '{{.State.Running}}' in args:
                inspects+=1;return 'false' if inspects==1 else 'true'
            return ''
        with patch.object(self.manager,'_command',side_effect=command),patch('operations.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='false')):
            self.manager.start('restore',{'file':'backup.zip','password':'secret','confirmation':'RESTORE'})
            self.assertEqual(self.wait_job()['state'],'failed')
        self.assertEqual((app/'data/original').read_text(),'original data')
        self.assertTrue(any(app.glob('data.failed-restore-*')))
        self.assertTrue(any('stop' in args and 'vaultwarden' in args for args in commands))

    def test_restore_rejects_symlink_before_stopping_service(self):
        self.backup()
        app,backup=self.manager.paths()
        archive=backup/'backups/backup.zip';archive.write_bytes(b'fixture')
        commands=[]
        def command(args,input_text=None,secret=False):
            commands.append(args)
            output=next((v[:-len(':/output')] for v in args if v.endswith(':/output')),None)
            if output:(Path(output)/'badlink').symlink_to(self.root/'outside')
            return ''
        with patch.object(self.manager,'_command',side_effect=command):
            self.manager.start('restore',{'file':'backup.zip','password':'secret','confirmation':'RESTORE'})
            self.assertEqual(self.wait_job()['state'],'failed')
        self.assertFalse(any('stop' in args for args in commands))
        with self.assertRaises(ValueError):self.manager.archive_path('../outside.zip')
        for value in ('../x','/etc/passwd','x/../../y','x\\y'):
            with self.assertRaises(ValueError):safe_relative(value)

    def test_uninstall_requires_typed_confirmation_and_keeps_data_by_default(self):
        self.backup()
        with self.assertRaises(ValueError):self.manager.start('remove-services',{'scope':'all','delete_data':True})
        commands=[]
        with patch.object(self.manager,'_command',side_effect=lambda args,**kw:commands.append(args) or ''):
            self.manager.start('remove-services',{'scope':'backup','delete_data':False,'confirmation':'DELETE'})
            self.assertEqual(self.wait_job()['state'],'success')
        self.assertTrue((self.manager.paths()[1]/'backup.env').exists())
        self.assertTrue((self.manager.paths()[0]/'data').exists())
        self.assertTrue(all('network' not in cmd for cmd in commands))


    def test_r2_remote_saves_verified_config_and_target(self):
        self.backup()
        backup=self.manager.paths()[1]
        config=backup/'vw-backup-config/rclone/rclone.conf'
        probed=[]
        def command(args,input_text=None,secret=False):
            if 'obscure' in args:
                self.assertEqual(input_text,'R2Secret!123\n')
                return 'obscured-r2-secret\n'
            raise AssertionError(args)
        def probe(args,config_file,secrets=(),label='',hint=''):
            probed.append((list(args),Path(config_file).read_text(),tuple(secrets)))
            return ''
        data=dict(preset='r2',name='R2Backup',account_id='0123456789ABCDEF0123456789ABCDEF',access_key_id='AKIAEXAMPLE123456',
            secret_access_key='R2Secret!123',bucket='vw-backup',path='/VaultBackup',no_check_bucket=True,set_target=True)
        with patch.object(self.manager,'_command',side_effect=command),patch.object(self.manager,'rclone_probe',side_effect=probe):
            self.manager.start('remote-preset-save',data)
            self.assertEqual(self.wait_job()['state'],'success')
        written=config.read_text()
        self.assertIn('[R2Backup]',written)
        self.assertIn('[BitwardenBackup]',written)
        self.assertIn('provider = Cloudflare',written)
        self.assertIn('access_key_id = AKIAEXAMPLE123456',written)
        self.assertIn('secret_access_key = obscured-r2-secret',written)
        self.assertIn('endpoint = https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com',written)
        self.assertIn('no_check_bucket = true',written)
        # The subpath is created by the first backup, so verification uses the bucket root.
        self.assertEqual(probed[0][0],['lsf','R2Backup:vw-backup','--max-depth','1'])
        self.assertIn('[R2Backup]',probed[0][1])
        self.assertIn('obscured-r2-secret',probed[0][2])
        env=read_vars(backup/'backup.env')
        self.assertEqual(env['RCLONE_REMOTE_NAME'],'BitwardenBackup')
        self.assertEqual(env['RCLONE_REMOTE_NAME_1'],'R2Backup')
        self.assertEqual(env['RCLONE_REMOTE_DIR_1'],'/vw-backup/VaultBackup')
        public=json.dumps(self.manager.config())+json.dumps(self.manager.task())
        self.assertNotIn('R2Secret!123',public)
        self.assertNotIn('obscured-r2-secret',public)

    def test_r2_remote_failure_keeps_config_and_reuses_existing_secret(self):
        self.backup()
        backup=self.manager.paths()[1]
        config=backup/'vw-backup-config/rclone/rclone.conf'
        data=dict(preset='r2',name='R2Backup',account_id='0123456789abcdef0123456789abcdef',access_key_id='ACCESSKEY12345678',
            secret_access_key='FirstSecret!',bucket='vw-backup',path='',no_check_bucket=False,set_target=False)
        with patch.object(self.manager,'_command',return_value='obscured-one\n'),patch.object(self.manager,'rclone_probe',return_value=''):
            self.manager.start('remote-preset-save',data)
            self.assertEqual(self.wait_job()['state'],'success')
        first=config.read_text()
        def failed(args,config_file,secrets=(),label='',hint=''):
            raise ValueError('R2 连接验证失败：403 AccessDenied')
        with patch.object(self.manager,'_command',return_value='unused\n'),patch.object(self.manager,'rclone_probe',side_effect=failed):
            self.manager.start('remote-preset-save',dict(data,secret_access_key='',bucket='other-bucket'))
            self.assertEqual(self.wait_job()['state'],'failed')
        self.assertEqual(config.read_text(),first)
        with patch.object(self.manager,'_command',return_value='unused\n'),patch.object(self.manager,'rclone_probe',return_value=''):
            self.manager.start('remote-preset-save',dict(data,secret_access_key=''))
            self.assertEqual(self.wait_job()['state'],'success')
        self.assertIn('secret_access_key = obscured-one',config.read_text())

    def test_r2_remote_saves_selected_jurisdiction(self):
        self.backup()
        config=self.manager.paths()[1]/'vw-backup-config/rclone/rclone.conf'
        data=dict(preset='r2',name='R2EU',account_id='0'*32,access_key_id='ACCESSKEY12345678',secret_access_key='Secret!123',
            bucket='vw-backup',path='',jurisdiction='eu',no_check_bucket=True,set_target=False)
        with patch.object(self.manager,'_command',return_value='obscured\n'),patch.object(self.manager,'rclone_probe',return_value=''):
            self.manager.start('remote-preset-save',data)
            self.assertEqual(self.wait_job()['state'],'success')
        self.assertIn('endpoint = https://%s.eu.r2.cloudflarestorage.com'%('0'*32),config.read_text())
        self.assertIn('no_check_bucket = true',config.read_text())

    def test_r2_remote_validates_fields_and_rejects_conflicting_names(self):
        self.backup()
        base=dict(preset='r2',name='R2Backup',account_id='0'*32,access_key_id='ACCESSKEY12345678',
            secret_access_key='Secret!123',bucket='vw-backup',path='',no_check_bucket=False,set_target=False)
        for change in ({'account_id':'short'},{'bucket':'Bad Bucket'},{'bucket':'ab'},{'name':'bad name'},{'name':'DEFAULT'},{'name':'default'},{'jurisdiction':'bad zone'},{'path':'../escape'},{'bucket':'a'*63,'path':('b'*128+'/')*3+'b'*125}):
            with self.assertRaises(ValueError):
                self.manager.start('remote-preset-save',dict(base,**change))
        config=self.manager.paths()[1]/'vw-backup-config/rclone/rclone.conf'
        config.write_text(config.read_text()+'[ExistingS3]\ntype = s3\nprovider = AWS\n')
        with patch.object(self.manager,'_command',return_value='obscured\n'):
            self.manager.start('remote-preset-save',dict(base,name='ExistingS3'))
            self.assertEqual(self.wait_job()['state'],'failed')
        self.assertNotIn('ExistingS3]\ntype = s3\nprovider = Cloudflare',config.read_text())
        with patch.object(self.manager,'_command',return_value='obscured\n'),patch.object(self.manager,'rclone_probe',return_value=''):
            self.manager.start('remote-preset-save',dict(base,name='r2.backup'))
            self.assertEqual(self.wait_job()['state'],'success')
        self.assertIn('[r2.backup]',config.read_text())

    def test_r2_remote_refuses_to_overwrite_config_changed_during_probe(self):
        self.backup()
        backup=self.manager.paths()[1]
        config=backup/'vw-backup-config/rclone/rclone.conf'
        data=dict(preset='r2',name='R2Backup',account_id='0'*32,access_key_id='ACCESSKEY12345678',
            secret_access_key='Secret!123',bucket='vw-backup',path='',no_check_bucket=False,set_target=False)
        def probe(args,config_file,secrets=(),label='',hint=''):
            config.write_text(config.read_text()+'\n[Other]\ntype = webdav\n')
            return ''
        with patch.object(self.manager,'_command',return_value='obscured\n'),patch.object(self.manager,'rclone_probe',side_effect=probe):
            self.manager.start('remote-preset-save',data)
            self.assertEqual(self.wait_job()['state'],'failed')
        written=config.read_text()
        self.assertIn('[Other]',written)
        self.assertNotIn('[R2Backup]',written)

    def test_rclone_probe_redacts_other_secrets_and_parse_lines(self):
        self.backup()
        trial=self.manager.state/'rclone.conf'
        trial.write_text('[Other]\ntype = webdav\npass = OtherSecret\n')
        def fake_run(args,stdout=None,stderr=None,timeout=None):
            stderr.write(b'Failed to load config file "x": could not parse line: pass = OtherSecret\nERROR: 403 AccessDenied access_key_id=AKIAOTHER\nERROR: parse error in https://alice:S3cretToken@dav.example.com/dav/?token=abc\n')
            stderr.flush()
            return SimpleNamespace(returncode=1)
        with patch('operations.subprocess.run',side_effect=fake_run):
            with self.assertRaises(ValueError) as raised:
                self.manager.rclone_probe(['lsf','R2:vw-backup'],trial,secrets=('CurrentSecret',))
        message=str(raised.exception)
        self.assertNotIn('OtherSecret',message)
        self.assertNotIn('AKIAOTHER',message)
        self.assertNotIn('S3cretToken',message)
        self.assertNotIn('token=abc',message)

    def test_r2_remote_rolls_back_when_target_save_fails(self):
        self.backup()
        backup=self.manager.paths()[1]
        config=backup/'vw-backup-config/rclone/rclone.conf'
        env=backup/'backup.env'
        original=config.read_text();env_original=env.read_text()
        data=dict(preset='r2',name='R2Backup',account_id='0'*32,access_key_id='ACCESSKEY12345678',
            secret_access_key='Secret!123',bucket='vw-backup',path='',no_check_bucket=False,set_target=True)
        with patch.object(self.manager,'_command',return_value='obscured\n'),patch.object(self.manager,'rclone_probe',return_value=''),patch.object(self.manager,'save_extended',side_effect=ValueError('target write failed')):
            self.manager.start('remote-preset-save',data)
            self.assertEqual(self.wait_job()['state'],'failed')
        self.assertEqual(config.read_text(),original)
        self.assertEqual(env.read_text(),env_original)

    def test_r2_remote_saves_without_backup_plan_and_skips_target(self):
        self.configured()
        backup=self.manager.paths()[1]
        config=backup/'vw-backup-config/rclone/rclone.conf'
        data=dict(preset='r2',name='R2Backup',account_id='0'*32,access_key_id='ACCESSKEY12345678',
            secret_access_key='Secret!123',bucket='vw-backup',path='',no_check_bucket=False,set_target=True)
        with patch.object(self.manager,'_command',return_value='obscured\n'),patch.object(self.manager,'rclone_probe',return_value=''):
            self.manager.start('remote-preset-save',data)
            job=self.wait_job();self.assertEqual(job['state'],'success')
        self.assertIn('备份计划尚未保存',job['output'])
        self.assertIn('[R2Backup]',config.read_text())
        self.assertFalse((backup/'backup.env').exists())

    def test_r2_remote_reconfigure_reuses_credentials_and_eu_endpoint(self):
        self.backup()
        backup=self.manager.paths()[1]
        config=backup/'vw-backup-config/rclone/rclone.conf'
        account='a'*32
        config.write_text(config.read_text()+'\n[R2Backup]\ntype = s3\nprovider = Cloudflare\naccess_key_id = ACCESSKEY12345678\nsecret_access_key = obscured-old\nchunk_size = 10M\nendpoint = https://%s.eu.r2.cloudflarestorage.com\n'%account)
        data=dict(preset='r2',name='R2Backup',account_id='',access_key_id='',secret_access_key='',bucket='vw-backup',path='',no_check_bucket=False,set_target=False)
        with patch.object(self.manager,'rclone_probe',return_value=''):
            self.manager.start('remote-preset-save',data)
            self.assertEqual(self.wait_job()['state'],'success')
        written=config.read_text()
        self.assertIn('endpoint = https://%s.eu.r2.cloudflarestorage.com'%account,written)
        self.assertIn('access_key_id = ACCESSKEY12345678',written)
        self.assertIn('secret_access_key = obscured-old',written)
        self.assertIn('chunk_size = 10M',written)

    def test_preset_secret_preserves_leading_and_trailing_spaces(self):
        self.backup()
        seen=[]
        def command(args,input_text=None,secret=False):
            if 'obscure' in args:
                seen.append(input_text);return 'obscured-space\n'
            raise AssertionError(args)
        data=dict(preset='r2',name='SpaceSecret',account_id='0'*32,access_key_id='ACCESSKEY12345678',
            secret_access_key=' spaced secret ',bucket='vw-backup',path='',no_check_bucket=False,set_target=False)
        with patch.object(self.manager,'_command',side_effect=command),patch.object(self.manager,'rclone_probe',return_value=''):
            self.manager.start('remote-preset-save',data)
            self.assertEqual(self.wait_job()['state'],'success')
        self.assertEqual(seen,[' spaced secret \n'])

    def test_preset_webdav_and_s3_reject_credentials_in_urls(self):
        self.backup()
        base={'preset':'webdav','name':'Dav','vendor':'other','user':'me','pass':'Secret!123','directory':'/VaultBackup','set_target':False}
        for url in ('https://user:pass@dav.example.com/dav/','https://dav.example.com/dav/?token=abc','https://dav.example.com/dav/#frag'):
            with self.assertRaises(ValueError):
                self.manager.start('remote-preset-save',dict(base,url=url))
        s3={'preset':'s3','name':'S3','provider':'Minio','access_key_id':'ACCESSKEY12345678','secret_access_key':'Secret!123',
            'region':'us-east-1','bucket':'vw-backup','path':'','no_check_bucket':False,'set_target':False}
        for endpoint in ('https://user:pass@s3.example.com','https://s3.example.com?token=abc','https://s3.example.com/#frag'):
            with self.assertRaises(ValueError):
                self.manager.start('remote-preset-save',dict(s3,endpoint=endpoint))

    def test_config_masks_remote_url_credentials(self):
        self.backup()
        backup=self.manager.paths()[1]
        config=backup/'vw-backup-config/rclone/rclone.conf'
        config.write_text(config.read_text()+'\n[Leaky]\ntype = webdav\nurl = https://user:pass@dav.example.com/dav/?token=abc\nuser = me\npass = obscured\n')
        public=json.dumps(self.manager.config())
        self.assertNotIn('user:pass',public)
        self.assertNotIn('token=abc',public)
        update_vars(backup/'backup.env',{'RCLONE_REMOTE_NAME':'Leaky','RCLONE_REMOTE_DIR':'/VaultBackup'})
        public=json.dumps(self.manager.config())
        self.assertNotIn('user:pass',public)
        self.assertNotIn('token=abc',public)

    def test_preset_remembers_target_for_later_reconfigure(self):
        self.backup()
        data=dict(preset='r2',name='R2Backup',account_id='0'*32,access_key_id='ACCESSKEY12345678',
            secret_access_key='Secret!123',bucket='vw-backup',path='VaultBackup',no_check_bucket=False,set_target=False)
        with patch.object(self.manager,'_command',return_value='obscured\n'),patch.object(self.manager,'rclone_probe',return_value=''):
            self.manager.start('remote-preset-save',data)
            self.assertEqual(self.wait_job()['state'],'success')
        remotes={item['name']:item for item in self.manager.config()['extras']['remotes']}
        self.assertEqual(remotes['R2Backup']['target'],'/vw-backup/VaultBackup')
        self.assertTrue((self.manager.state/'storage-targets.json').exists())

    def test_preset_s3_saves_and_verifies(self):
        self.backup()
        backup=self.manager.paths()[1]
        config=backup/'vw-backup-config/rclone/rclone.conf'
        probed=[]
        def probe(args,config_file,secrets=(),label='',hint=''):
            probed.append((list(args),label));return ''
        data=dict(preset='s3',name='MinioCloud',provider='Minio',access_key_id='MINIOACCESS1234',
            secret_access_key='MinioSecret!',region='us-east-1',endpoint='https://minio.example.com',
            bucket='vw-backup',path='VaultBackup',no_check_bucket=False,set_target=True)
        with patch.object(self.manager,'_command',return_value='obscured-minio\n'),patch.object(self.manager,'rclone_probe',side_effect=probe):
            self.manager.start('remote-preset-save',data)
            self.assertEqual(self.wait_job()['state'],'success')
        written=config.read_text()
        self.assertIn('[MinioCloud]',written)
        self.assertIn('provider = Minio',written)
        self.assertIn('endpoint = https://minio.example.com',written)
        self.assertIn('region = us-east-1',written)
        self.assertIn('secret_access_key = obscured-minio',written)
        # The subpath is created by the first backup, so verification uses the bucket root.
        self.assertEqual(probed[0][0],['lsf','MinioCloud:vw-backup','--max-depth','1'])
        self.assertEqual(probed[0][1],'S3 兼容对象存储')
        env=read_vars(backup/'backup.env')
        self.assertEqual(env['RCLONE_REMOTE_NAME_1'],'MinioCloud')
        self.assertEqual(env['RCLONE_REMOTE_DIR_1'],'/vw-backup/VaultBackup')

    def test_preset_webdav_creates_target_directory(self):
        self.backup()
        backup=self.manager.paths()[1]
        config=backup/'vw-backup-config/rclone/rclone.conf'
        probed=[]
        def probe(args,config_file,secrets=(),label='',hint=''):
            probed.append(list(args));return ''
        data={'preset':'webdav','name':'WebDAVCloud','url':'https://dav.example.com/dav/','vendor':'nextcloud',
            'user':'me','pass':'WebdavSecret!','directory':'/VaultBackup','set_target':True}
        with patch.object(self.manager,'_command',return_value='obscured-pass\n'),patch.object(self.manager,'rclone_probe',side_effect=probe):
            self.manager.start('remote-preset-save',data)
            self.assertEqual(self.wait_job()['state'],'success')
        written=config.read_text()
        self.assertIn('[WebDAVCloud]',written)
        self.assertIn('type = webdav',written)
        self.assertIn('url = https://dav.example.com/dav/',written)
        self.assertIn('vendor = nextcloud',written)
        self.assertIn('pass = obscured-pass',written)
        self.assertEqual(probed,[['mkdir','WebDAVCloud:/VaultBackup'],['lsd','WebDAVCloud:/VaultBackup']])
        env=read_vars(backup/'backup.env')
        self.assertEqual(env['RCLONE_REMOTE_NAME_1'],'WebDAVCloud')
        self.assertEqual(env['RCLONE_REMOTE_DIR_1'],'/VaultBackup')

    def test_preset_sftp_saves_relative_directory(self):
        self.backup()
        backup=self.manager.paths()[1]
        config=backup/'vw-backup-config/rclone/rclone.conf'
        probed=[]
        def probe(args,config_file,secrets=(),label='',hint=''):
            probed.append(list(args));return ''
        data={'preset':'sftp','name':'SftpCloud','host':'sftp.example.com','port':'2222','user':'me',
              'pass':'SftpSecret!','directory':'backup','set_target':True}
        with patch.object(self.manager,'_command',return_value='obscured-sftp\n'),patch.object(self.manager,'rclone_probe',side_effect=probe):
            self.manager.start('remote-preset-save',data)
            self.assertEqual(self.wait_job()['state'],'success')
        written=config.read_text()
        self.assertIn('[SftpCloud]',written)
        self.assertIn('type = sftp',written)
        self.assertIn('host = sftp.example.com',written)
        self.assertIn('port = 2222',written)
        self.assertIn('user = me',written)
        self.assertIn('pass = obscured-sftp',written)
        self.assertEqual(probed,[['mkdir','SftpCloud:backup'],['lsd','SftpCloud:backup']])
        env=read_vars(backup/'backup.env')
        self.assertEqual(env['RCLONE_REMOTE_DIR_1'],'backup')

    def test_preset_local_saves_without_probe(self):
        self.backup()
        backup=self.manager.paths()[1]
        config=backup/'vw-backup-config/rclone/rclone.conf'
        probed=[]
        data=dict(preset='local',name='LocalMirror',directory='/bitwarden/backup/archive',set_target=True)
        with patch.object(self.manager,'rclone_probe',side_effect=lambda *args,**kwargs:probed.append(args) or ''):
            self.manager.start('remote-preset-save',data)
            self.assertEqual(self.wait_job()['state'],'success')
        self.assertFalse(probed)
        self.assertIn('[LocalMirror]\ntype = local',config.read_text())
        env=read_vars(backup/'backup.env')
        self.assertEqual(env['RCLONE_REMOTE_NAME_1'],'LocalMirror')
        self.assertEqual(env['RCLONE_REMOTE_DIR_1'],'/bitwarden/backup/archive')

    def test_preset_s3_requires_endpoint_for_non_aws(self):
        self.backup()
        base=dict(preset='s3',name='MinioCloud',provider='Minio',access_key_id='ACCESSKEY12345678',
            secret_access_key='Secret!123',region='us-east-1',endpoint='',bucket='vw-backup',path='',set_target=False)
        with self.assertRaises(ValueError):
            self.manager.start('remote-preset-save',base)

    def test_preset_s3_rejects_provider_change_on_existing_remote(self):
        self.backup()
        config=self.manager.paths()[1]/'vw-backup-config/rclone/rclone.conf'
        config.write_text(config.read_text()+'[MinioCloud]\ntype = s3\nprovider = Minio\nendpoint = https://minio.example.com\naccess_key_id = ACCESSKEY12345678\nsecret_access_key = obscured\n')
        original=config.read_text()
        data=dict(preset='s3',name='MinioCloud',provider='AWS',access_key_id='',secret_access_key='',
            region='us-east-1',endpoint='',bucket='vw-backup',path='',no_check_bucket=False,set_target=False)
        with patch.object(self.manager,'_command',return_value='obscured\n'):
            self.manager.start('remote-preset-save',data)
            self.assertEqual(self.wait_job()['state'],'failed')
        self.assertEqual(config.read_text(),original)

    def test_remote_r2_save_alias_still_saves(self):
        self.backup()
        config=self.manager.paths()[1]/'vw-backup-config/rclone/rclone.conf'
        data=dict(name='R2Alias',account_id='0'*32,access_key_id='ACCESSKEY12345678',
            secret_access_key='Secret!123',bucket='vw-backup',path='',no_check_bucket=True,set_target=False)
        with patch.object(self.manager,'_command',return_value='obscured\n'),patch.object(self.manager,'rclone_probe',return_value=''):
            self.manager.start('remote-r2-save',data)
            self.assertEqual(self.wait_job()['state'],'success')
        self.assertIn('[R2Alias]',config.read_text())

    def test_storage_preset_catalog_exposed(self):
        self.backup()
        presets={item['id']:item for item in self.manager.config()['extras']['storage_presets']}
        self.assertTrue({'r2','s3','webdav','sftp','ftp','local'}.issubset(set(presets)))
        self.assertTrue(any(field['key']=='account_id' for field in presets['r2']['fields']))
        self.assertTrue(any(field['key']=='bucket' and field.get('required') for field in presets['s3']['fields']))

class InstallerDefaultsTests(unittest.TestCase):
    def test_empty_password_generates_secret_without_confirmation_and_not_in_hash_file(self):
        spec=importlib.util.spec_from_file_location('defaults_panel',ROOT/'web/server.py')
        panel=importlib.util.module_from_spec(spec);spec.loader.exec_module(panel)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory);state=path/'state';secret=path/'secret'
            with patch.object(sys,'argv',['panel','configure','--state-dir',str(state),'--port','18373','--bootstrap-secret-file',str(secret)]),\
                 patch.object(panel.getpass,'getpass',return_value='') as prompt,\
                 patch.object(panel,'digest',return_value='hashed-value'),contextlib.redirect_stdout(io.StringIO()):
                panel.main()
            password=secret.read_text()
            self.assertGreaterEqual(len(password),24)
            self.assertEqual(prompt.call_count,1)
            self.assertNotIn(password,(state/'credentials.json').read_text())
            self.assertEqual(os.stat(secret).st_mode&0o777,0o600)

    def test_terminal_uninstall_removes_panel_only_and_cancel_has_no_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            app=root/'panel';state=root/'state';unit=root/'units';cli_dir=root/'cli';bin_dir=root/'bin'
            for path in (app,state,unit,cli_dir,bin_dir):path.mkdir()
            vault=root/'vault-data';vault.write_text('keep vault data')
            source=(ROOT/'panel-cli.sh').read_text()
            for old,new in [('/opt/vw-panel',str(app)),('/var/lib/vw-panel',str(state)),('/etc/systemd/system',str(unit)),('/usr/local/bin/vwp',str(cli_dir/'vwp'))]:source=source.replace(old,new)
            source=source.replace('[[ "$EUID" -eq 0 ]]','true')
            file=cli_dir/'vwp';file.write_text(source)
            (unit/'vw-panel.service').write_text('fixture')
            fake=bin_dir/'systemctl';fake.write_text('#!/bin/sh\nexit 0\n');fake.chmod(0o755)
            # macOS does not ship flock. A test stub is sufficient for this path/removal test.
            flock=bin_dir/'flock';flock.write_text('#!/bin/sh\nexit 0\n');flock.chmod(0o755)
            env=dict(os.environ,PATH=str(bin_dir)+':'+os.environ['PATH'])
            cancel=subprocess.run(['bash',str(file),'uninstall'],input='no\n',text=True,capture_output=True,env=env)
            self.assertEqual(cancel.returncode,0,cancel.stderr);self.assertTrue(app.exists())
            result=subprocess.run(['bash',str(file),'uninstall'],input='UNINSTALL\nn\n',text=True,capture_output=True,env=env)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertFalse(app.exists());self.assertFalse(file.exists())
            self.assertTrue(state.exists());self.assertEqual(vault.read_text(),'keep vault data')

if __name__=='__main__':unittest.main()

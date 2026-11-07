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

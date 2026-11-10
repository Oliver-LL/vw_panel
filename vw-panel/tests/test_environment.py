import json
import os
from pathlib import Path
import unittest
import tempfile
from unittest.mock import patch
from types import SimpleNamespace
import test_manager
from manager import read_vars, update_vars, write_private
from environment import CATALOG, definitions

class EnvironmentTests(unittest.TestCase):
    setUp=test_manager.ManagerTests.setUp
    tearDown=test_manager.ManagerTests.tearDown
    configured=test_manager.ManagerTests.configured

    def backup(self,**extra):
        self.manager.save('backup',dict(CRON='0 3 * * *',TIMEZONE='Asia/Shanghai',BACKUP_KEEP_DAYS='30',ZIP_PASSWORD='backup-password-123',**extra))
        return self.manager.paths()[1]/'backup.env'

    def test_documented_catalog_and_managed_fields(self):
        self.assertEqual(len(CATALOG['core']),144)
        self.assertEqual(len(CATALOG['backup']),32)
        for scope,keys in [('core',['TZ','DOMAIN','LOGIN_RATELIMIT_SECONDS','ADMIN_TOKEN','ENABLE_WEBSOCKET','SMTP_HOST','SSO_CLIENT_SECRET']),('backup',['ZIP_TYPE','PING_URL_WHEN_FAILURE','DATA_DB','RCLONE_REMOTE_NAME','MAIL_SMTP_VARIABLES'])]:
            for key in keys:self.assertIn(key,definitions(scope))
        self.assertEqual(definitions('backup')['RCLONE_REMOTE_NAME']['page'],'targets')
        self.assertEqual(definitions('backup')['RCLONE_REMOTE_DIR']['location'],'备份目标与保存目录')
        env=self.configured();original=env.read_text()
        for key in ['DOMAIN','ADMIN_TOKEN','ROCKET_ADDRESS','DATA_FOLDER','WEBSOCKET_ENABLED','PUSH_INSTALLATION_KEY']:
            with self.subTest(key=key),self.assertRaises(ValueError):self.manager.save('core-env',{'values':{key:'anything'}})
        self.assertEqual(original,env.read_text())
        with self.assertRaises(ValueError):self.manager.save('core-env',{'values':{'NOT_DOCUMENTED':'anything'}})

    def test_scoped_edits_secret_retention_and_removal(self):
        env=self.configured();backup=self.backup()
        update_vars(env,{'UNKNOWN':'preserved','SMTP_PASSWORD':'smtp-secret'})
        self.manager.save('core-env',{'values':{'SSO_ENABLED':'true','SSO_CLIENT_SECRET':"secret-$'\\value",'SMTP_TIMEOUT':'45'}})
        values=read_vars(env);self.assertEqual(values['SSO_CLIENT_SECRET'],"secret-$'\\value")
        self.assertEqual(values['UNKNOWN'],'preserved');self.assertEqual(values['SMTP_PASSWORD'],'smtp-secret')
        self.manager.save('core-env',{'values':{'SSO_CLIENT_SECRET':''},'remove':['SMTP_TIMEOUT']})
        self.assertNotIn('SMTP_TIMEOUT',read_vars(env));self.assertEqual(read_vars(env)['SSO_CLIENT_SECRET'],values['SSO_CLIENT_SECRET'])
        self.manager.save('backup-env',{'values':{'PING_URL_WHEN_FAILURE':'https://notify.example/?token=webhook-secret'}})
        self.assertIn('PING_URL_WHEN_FAILURE',read_vars(backup));self.assertNotIn('PING_URL_WHEN_FAILURE',read_vars(env))
        self.manager.save('core-env',{'remove':['SSO_CLIENT_SECRET']})
        self.assertNotIn('SSO_CLIENT_SECRET',read_vars(env));self.assertEqual(os.stat(env).st_mode&0o777,0o600)
        self.assertEqual(self.manager.application_state()['backup'],'pending')

    def test_invalid_updates_are_atomic(self):
        env=self.configured();original=env.read_text()
        bad=[{'values':{'SMTP_TIMEOUT':'-1'}},{'values':{'SSO_ENABLED':'yes'}},{'values':{'LOG_LEVEL':'debug\nDOMAIN=bad'}},{'values':{'SSO_CLIENT_SECRET':''}},{'values':{'LOG_LEVEL':5}},{'values':{'LOG_LEVEL':'warn'},'remove':['LOG_LEVEL']},{'remove':['ADMIN_TOKEN']},{'remove':[{}]}]
        for data in bad:
            with self.subTest(data=data),self.assertRaises(ValueError):self.manager.save('core-env',data)
            self.assertEqual(original,env.read_text())

    def test_public_configuration_masks_secrets_and_reports_overrides(self):
        env=self.configured();bk=self.backup()
        update_vars(env,{'DATABASE_URL':'postgres://user:private-db-pass@host/db','SSO_CLIENT_SECRET':'private-sso-secret','LOG_LEVEL':'warn','FUTURE_SECRET':'private-unknown-secret'})
        update_vars(bk,{'PING_URL':'https://notify/?token=private-hook-secret','RCLONE_GLOBAL_FLAG':'--header X-private-header'})
        write_private(env.parent/'data/config.json',json.dumps({'log_level':'debug','sso_client_secret':'admin-config-secret'}))
        config=self.manager.config();raw=json.dumps(config)
        for value in ['private-db-pass','private-sso-secret','private-unknown-secret','private-hook-secret','X-private-header','admin-config-secret']:self.assertNotIn(value,raw)
        specs={row['key']:row for row in config['extras']['environment']['core']}
        self.assertTrue(specs['LOG_LEVEL']['overridden']);self.assertTrue(specs['SSO_CLIENT_SECRET']['present']);self.assertEqual(specs['SSO_CLIENT_SECRET']['value'],'')
        self.assertEqual(specs['LOG_LEVEL']['value'],'warn');self.assertFalse(specs['FUTURE_SECRET']['editable'])

    def test_output_hides_credentials_without_redacting_flags_or_counters(self):
        env=self.configured();bk=self.backup()
        update_vars(env,{'SSO_DEBUG_TOKENS':'true','PASSWORD_ITERATIONS':'600000','EMAIL_TOKEN_SIZE':'6','DATABASE_URL':'postgres://user:privatepass@db/vault'})
        update_vars(bk,{'PING_URL':'https://hook/?token=privatehook'})
        message='true 600000 6 postgres://user:privatepass@db/vault https://hook/?token=privatehook'
        def run(args,**kwargs):
            kwargs['stdout'].write(message.encode());return SimpleNamespace(returncode=0)
        self.manager.logfile=tempfile.TemporaryFile()
        with patch('manager.subprocess.run',side_effect=run):output=self.manager._command(['docker','inspect'])
        self.assertIn('true 600000 6',output);self.assertNotIn('privatepass',output);self.assertNotIn('privatehook',output)
        self.manager.logfile.seek(0);self.assertNotIn(b'privatehook',self.manager.logfile.read())

    def test_login_limits_and_canonical_websocket(self):
        env=self.configured();update_vars(env,{'WEBSOCKET_ENABLED':'false'})
        self.assertEqual(self.manager.config()['extras']['advanced']['ENABLE_WEBSOCKET'],'false')
        self.manager.save('advanced',{'LOGIN_RATELIMIT_MAX_BURST':'10','LOGIN_RATELIMIT_SECONDS':'60','WEBSOCKET_ENABLED':True,'TZ':'UTC'})
        values=read_vars(env);self.assertEqual(values['ENABLE_WEBSOCKET'],'true');self.assertEqual(values['LOGIN_RATELIMIT_MAX_BURST'],'10')
        self.assertEqual(self.manager.config()['extras']['advanced']['ENABLE_WEBSOCKET'],'true')

    def test_backup_archive_options_and_unencrypted_first_setup(self):
        self.configured()
        data=dict(CRON='0 3 * * *',TIMEZONE='UTC',BACKUP_KEEP_DAYS='10',ZIP_ENABLE=False,ZIP_TYPE='7z',BACKUP_FILE_SUFFIX='%Y%m%d_%H%M',DISPLAY_NAME='My Vault')
        self.manager.save('backup',data)
        env=self.manager.paths()[1]/'backup.env';values=read_vars(env)
        self.assertEqual(values['ZIP_ENABLE'],'FALSE');self.assertEqual(values['ZIP_TYPE'],'7z');self.assertEqual(values['BACKUP_FILE_SUFFIX'],'%Y%m%d_%H%M')
        self.assertFalse(self.manager.config()['backup']['ZIP_ENABLE'])
        with self.assertRaises(ValueError):self.manager.save('backup',{**data,'ZIP_ENABLE':True})
        self.manager.save('backup',{**data,'ZIP_ENABLE':True,'ZIP_PASSWORD':'backup-new-password'})
        self.assertTrue(self.manager.config()['backup']['ZIP_ENABLE'])
        before=env.read_text()
        for update in [{'ZIP_TYPE':'tar'},{'BACKUP_FILE_SUFFIX':'../oops'},{'ZIP_ENABLE':'FALSE'}]:
            with self.assertRaises(ValueError):self.manager.save('backup',{**data,**update})
            self.assertEqual(before,env.read_text())

if __name__=='__main__':unittest.main()

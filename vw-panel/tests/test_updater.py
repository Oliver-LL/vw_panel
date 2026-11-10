import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'web'))
from updater import Updates, manifest, private_json
from manager import update_vars

class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.state=self.root/'state';self.current=self.root/'current';self.state.mkdir();self.current.mkdir()
        self.old={'version':'0.5.0','build_id':'a'*64};(self.current/'version.json').write_text(json.dumps(self.old))
        self.body=b'#!/bin/bash\n# trusted fixture only\n'
        self.latest={'version':'0.5.1','build_id':'b'*64,'installer_sha256':hashlib.sha256(self.body).hexdigest()}
        self.updates=Updates(self.state,self.current)
        self.latest['commit']='d'*40
        self.revision=patch('updater.revision',return_value='d'*40);self.revision.start();self.addCleanup(self.revision.stop)
    def tearDown(self):self.tmp.cleanup()
    def prepare(self):
        private_json(self.state/'update.json',dict(self.latest,state='upgrading',token='fixture',started=__import__('time').time()))

    def test_auto_check_cache_manual_check_and_no_false_latest_on_failure(self):
        with patch('updater.fetch',return_value=json.dumps(self.latest).encode()) as download:
            self.assertTrue(self.updates.check()['available']);self.updates.check();self.assertEqual(download.call_count,1)
            self.updates.check(force=True);self.assertEqual(download.call_count,2)
        with patch('updater.fetch',side_effect=OSError('offline')):
            result=self.updates.check(force=True)
        self.assertFalse(result['available']);self.assertTrue(result['error']);self.assertIsNone(result['latest'])

    def test_version_equality_uses_build_id_and_validates_manifest(self):
        with patch('updater.fetch',return_value=json.dumps(dict(self.latest,build_id='a'*64)).encode()):
            self.assertFalse(self.updates.check()['available'])
        for bad in ({},dict(self.latest,installer_sha256='wrong'),dict(self.latest,version='bash command')):
            with self.assertRaises(ValueError):manifest(bad)

    def test_background_worker_is_separate_and_version_is_pinned(self):
        with patch('updater.fetch',return_value=json.dumps(self.latest).encode()),patch('updater.subprocess.run',return_value=SimpleNamespace(returncode=0)) as run:
            self.updates.start(self.latest['build_id'])
        args=run.call_args.args[0]
        self.assertEqual(args[0],'systemd-run');self.assertIn('apply',args)
        self.assertTrue(self.updates.busy())
        self.assertEqual(os.stat(self.state/'update.json').st_mode&0o777,0o600)
        public=json.dumps(self.updates.progress());self.assertNotIn('token',public)
        with self.assertRaises(ValueError):self.updates.start(self.latest['build_id'])

    def test_stale_version_and_busy_operation_are_rejected(self):
        with patch('updater.fetch',return_value=json.dumps(self.latest).encode()):
            with self.assertRaises(ValueError):self.updates.start('c'*64)
            with (self.state/'operation.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                with self.assertRaises(ValueError):self.updates.start(self.latest['build_id'])
        self.assertFalse((self.state/'update.json').exists())

    def test_checksum_failure_never_executes_installer(self):
        self.prepare()
        with patch('updater.fetch',return_value=b'corrupt payload'),patch('updater.subprocess.run') as run:
            self.updates.apply('fixture')
        run.assert_not_called();self.assertEqual(self.updates.progress()['state'],'failed')
        self.assertEqual(self.updates.installed(),self.old)

    def test_successful_upgrade_uses_noninteractive_mode_and_keeps_credentials(self):
        self.prepare();credentials=self.state/'credentials.json';credentials.write_text('saved hash')
        def install(args,**kwargs):
            self.assertEqual(args[-1],'--upgrade');self.assertEqual(args[0],'bash')
            self.assertEqual(Path(args[1]).read_bytes(),self.body)
            (self.current/'version.json').write_text(json.dumps({'version':self.latest['version'],'build_id':self.latest['build_id']}))
            return SimpleNamespace(returncode=0)
        with patch('updater.fetch',return_value=self.body),patch('updater.subprocess.run',side_effect=install):self.updates.apply('fixture')
        self.assertEqual(self.updates.progress()['state'],'success');self.assertEqual(credentials.read_text(),'saved hash')
        self.assertFalse((self.state/'update-fixture').exists())

    def test_manifest_and_installer_are_bound_to_the_same_commit(self):
        urls=[]
        def download(url,maximum):
            urls.append(url)
            if url.endswith('release.json'):return json.dumps(self.latest).encode()
            if url.endswith('install-vw-web.sh'):return self.body
            raise AssertionError(url)
        with patch('updater.fetch',side_effect=download):
            checked=self.updates.check(force=True)
            private_json(self.state/'update.json',dict(checked['latest'],state='upgrading',token='fixture'))
            def install(*args,**kwargs):
                (self.current/'version.json').write_text(json.dumps(self.latest));return SimpleNamespace(returncode=0)
            with patch('updater.subprocess.run',side_effect=install):self.updates.apply('fixture')
        self.assertEqual(self.updates.progress()['state'],'success')
        self.assertEqual(len(urls),2);self.assertTrue(all('/'+('d'*40)+'/vw-panel/' in url for url in urls));self.assertTrue(all('/main/' not in url for url in urls))

    def test_legacy_request_rejects_changed_release_before_install(self):
        self.prepare();value=json.loads((self.state/'update.json').read_text());value.pop('commit');private_json(self.state/'update.json',value)
        with patch('updater.fetch',return_value=json.dumps(dict(self.latest,build_id='e'*64)).encode()),patch('updater.subprocess.run') as install:
            self.updates.apply('fixture')
        install.assert_not_called();self.assertEqual(self.updates.progress()['state'],'failed')

    def test_install_failure_keeps_old_version(self):
        self.prepare()
        with patch('updater.fetch',return_value=self.body),patch('updater.subprocess.run',return_value=SimpleNamespace(returncode=1)):
            self.updates.apply('fixture')
        self.assertEqual(self.updates.progress()['state'],'failed');self.assertEqual(self.updates.installed(),self.old)

    def test_rejected_systemd_launch_does_not_stay_busy(self):
        with patch('updater.fetch',return_value=json.dumps(self.latest).encode()),patch('updater.subprocess.run',return_value=SimpleNamespace(returncode=1)):
            with self.assertRaises(ValueError):self.updates.start(self.latest['build_id'])
        self.assertFalse(self.updates.busy());self.assertEqual(self.updates.progress()['state'],'failed')

class BashCompatibilityTests(unittest.TestCase):
    def test_shared_config_reader_and_raw_original_difference(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'web').symlink_to(ROOT/'web',target_is_directory=True)
            env=root/'vaultwarden.env';password="Abc$'\\Password!"
            update_vars(env,{'SIGNUPS_ALLOWED':'false','SMTP_PASSWORD':password})
            web=(ROOT/'vw_web.sh').read_text();original=(ROOT/'vw_original.sh').read_text()
            for source,expected in ((web,'false'),(original,"'false'")):
                function=re.search(r'^get_env_var\(\) \{.*?^\}',source,re.S|re.M).group()
                script=root/'reader.sh';script.write_text(function+'\nget_env_var "$1" SIGNUPS_ALLOWED\n')
                result=subprocess.run(['bash',str(script),str(env)],capture_output=True,text=True,check=True)
                self.assertEqual(result.stdout.strip(),expected)
            result=subprocess.run([sys.executable,str(ROOT/'web/manager.py'),'--export-env',str(env)],capture_output=True,text=True,check=True)
            self.assertIn('SMTP_PASSWORD='+password,result.stdout)

    def test_vwp_is_the_only_new_shortcut(self):
        installer=(ROOT/'installer-header.sh').read_text();cli=(ROOT/'panel-cli.sh').read_text()
        self.assertIn('/usr/local/bin/vwp',installer)
        self.assertNotIn('/usr/local/bin/vmp',installer+cli)
        self.assertIn('兼容',cli)
        self.assertIn('sudo vwp',cli)

if __name__=='__main__':unittest.main()

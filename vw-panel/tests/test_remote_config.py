import json
import os
from pathlib import Path
import subprocess
import threading
import time
from unittest.mock import patch
import unittest
import test_operations as fixtures
import remote_config

class RemoteConfigTests(unittest.TestCase):
    setUp=fixtures.OperationsTests.setUp
    tearDown=fixtures.OperationsTests.tearDown
    configured=fixtures.OperationsTests.configured
    backup=fixtures.OperationsTests.backup
    wait_job=fixtures.OperationsTests.wait_job

    def prepare(self):
        self.backup();self.config=self.manager.paths()[1]/'vw-backup-config/rclone/rclone.conf'
        folder=self.manager.state/'wizard-test';(folder/'rclone').mkdir(parents=True)
        (folder/'rclone/rclone.conf').write_text(self.config.read_text())
        self.manager.remote_wizard={'id':'wizard','state':'teamdrive','folder':str(folder),'name':'GoogleDrive','type':'drive','created':time.time(),'original':self.config.read_text(),'choices':['true','false']}
        return folder

    def test_empty_shared_drive_choice_is_rejected_without_network_request(self):
        self.prepare()
        with patch('remote_config.subprocess.Popen') as process:
            self.manager.start('remote-continue',{'wizard_id':'wizard','answer':''})
            job=self.wait_job();self.assertEqual(job['state'],'failed');self.assertIn('选择',job['error']);process.assert_not_called()

    def test_timeout_cleans_only_owned_container_and_releases_task_lock(self):
        self.prepare();original=self.config.read_text()
        class Process:
            returncode=None
            def __init__(self,args,**kwargs):self.calls=0
            def communicate(self,*args,**kwargs):
                self.calls+=1
                if self.calls==1:raise subprocess.TimeoutExpired('private-command',60)
            def kill(self):self.returncode=-9
        with patch('remote_config.subprocess.Popen',side_effect=Process),patch('remote_config.subprocess.run') as cleanup:
            self.manager.start('remote-continue',{'wizard_id':'wizard','answer':'false'})
            job=self.wait_job();self.assertEqual(job['state'],'failed');self.assertIn('60 秒',job['error'])
            command=cleanup.call_args.args[0];self.assertEqual(command[:3],['docker','rm','-f']);self.assertTrue(command[3].startswith('vwp-config-'));self.assertEqual(len(command),4)
        with self.manager.exclusive():pass
        self.assertEqual(self.config.read_text(),original)

    def test_cancel_interrupts_configuration_but_rejects_other_or_stale_tasks(self):
        self.prepare();ready=threading.Event();ended=threading.Event();original=self.config.read_text()
        class Process:
            returncode=None
            def __init__(self,args,**kwargs):ready.set()
            def communicate(self,*args,**kwargs):ended.wait(3)
            def terminate(self):self.returncode=-15;ended.set()
        with patch('remote_config.subprocess.Popen',side_effect=Process),patch('remote_config.subprocess.run'):
            self.manager.start('remote-continue',{'wizard_id':'wizard','answer':'false'});self.assertTrue(ready.wait(2))
            job_id=self.manager.job['id']
            with self.assertRaises(ValueError):remote_config.cancel(self.manager,'stale-id')
            result=remote_config.cancel(self.manager,job_id);self.assertTrue(result['ok'])
            job=self.wait_job();self.assertEqual(job['state'],'failed');self.assertIn('已取消',job['error'])
        with self.manager.exclusive():pass
        self.assertEqual(self.config.read_text(),original)
        for action in ('deploy','restore','backup-now'):
            self.manager.job={'id':'protected','state':'running','action':action}
            with self.assertRaisesRegex(ValueError,'不能中断'):remote_config.cancel(self.manager,'protected')

    def test_shared_drive_question_keeps_false_and_does_not_save_on_error(self):
        self.prepare();original=self.config.read_text()
        value={'State':'teamdrive','Option':{'Name':'config_change_team_drive','Exclusive':True,'Default':False,'Examples':[{'Value':'true','Help':'Yes'},{'Value':'false','Help':'No'}]}}
        with patch.object(self.manager,'rc',return_value=value):
            self.manager.start('remote-continue',{'wizard_id':'wizard','answer':'false'});job=self.wait_job();self.assertEqual(job['state'],'success');self.assertIs(job['result']['question']['Default'],False)
        with patch.object(self.manager,'rc',return_value={'Error':'PrivateFailure'}):
            self.manager.start('remote-continue',{'wizard_id':'wizard','answer':'false'});job=self.wait_job();self.assertEqual(job['state'],'failed')
        self.assertEqual(self.config.read_text(),original)

    def test_cancelling_idle_wizard_does_not_resurrect_question_after_refresh(self):
        folder=self.prepare();original=self.config.read_text()
        self.manager.job={'id':'idle-wizard','state':'success','action':'remote-continue','result':{'question':{'Name':'config_change_team_drive'}}}
        result=remote_config.cancel(self.manager,'idle-wizard')
        self.assertTrue(result['ok']);self.assertIsNone(self.manager.remote_wizard);self.assertFalse(folder.exists())
        self.assertNotIn('question',self.manager.job['result']);self.assertEqual(self.config.read_text(),original)

    def test_null_option_completion_saves_config_for_guided_and_full_wizards(self):
        folder=self.prepare()
        candidate=folder/'rclone/rclone.conf'
        candidate.write_text(candidate.read_text()+'\n[GoogleDrive]\ntype = drive\ntoken = fixture-only-token\n')
        for guided in (True,False):
            if not folder.exists():
                (folder/'rclone').mkdir(parents=True);candidate.write_text(self.config.read_text())
            self.manager.remote_wizard={'id':'wizard','state':'teamdrive_ok','folder':str(folder),'name':'GoogleDrive','type':'drive','created':time.time(),'original':self.config.read_text(),'choices':['true','false'],'guided':guided}
            with patch.object(self.manager,'rc',return_value={'Error':'','Option':None,'Result':'','State':''}):
                self.manager.start('remote-continue',{'wizard_id':'wizard','answer':'false'})
                job=self.wait_job();self.assertEqual(job['state'],'success');self.assertTrue(job['result']['done'])
            self.assertIn('[GoogleDrive]',self.config.read_text());self.assertIsNone(self.manager.remote_wizard)
            self.assertNotIn('fixture-only-token',json.dumps(job))
            self.manager.task_worker.join(2)
            with self.manager.exclusive():pass

    def test_unexpected_exception_finishes_task_and_allows_next_task(self):
        self.prepare()
        with patch.object(self.manager,'run_extended',side_effect=AttributeError('private-exception-fixture')):
            self.manager.start('remote-providers',{});job=self.wait_job();self.assertEqual(job['state'],'failed')
        self.assertNotIn('private-exception-fixture',json.dumps(job));self.assertIn('AttributeError',job['output'])
        self.manager.task_worker.join(2)
        with patch.object(self.manager,'run_extended',return_value=None):
            self.manager.start('remote-providers',{});self.assertEqual(self.wait_job()['state'],'success')

    def test_dead_worker_state_is_recovered_but_live_worker_is_not_interrupted(self):
        self.manager.job={'id':'tracked-job','state':'running'};self.manager.task_worker_job='tracked-job'
        class Worker:
            alive=True
            def is_alive(self):return self.alive
        worker=Worker();self.manager.task_worker=worker
        self.assertEqual(self.manager.task()['state'],'running')
        worker.alive=False
        self.assertEqual(self.manager.task()['state'],'failed')

    @unittest.skipUnless(os.environ.get('VWP_TEST_RCLONE'),'Optional real Rclone executable not supplied')
    def test_real_rclone_personal_drive_final_step(self):
        folder=self.prepare();candidate=folder/'rclone/rclone.conf'
        candidate.write_text(candidate.read_text()+'\n[GoogleDrive]\ntype = drive\ntoken = {"access_token":"fixture-only-access","token_type":"Bearer","refresh_token":"fixture-only-refresh","expiry":"2099-01-01T00:00:00Z"}\n')
        self.manager.remote_wizard.update(state='teamdrive_ok',guided=True)
        def rc(path,data,folder):
            result=subprocess.run([os.environ['VWP_TEST_RCLONE'],'rc','--loopback','--config',str(folder/'rclone/rclone.conf'),path,'--json',json.dumps(data)],capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0)
            value=json.loads(result.stdout);self.assertIsNone(value['Option']);self.assertEqual(value['State'],'');return value
        with patch.object(self.manager,'rc',side_effect=rc):
            self.manager.start('remote-continue',{'wizard_id':'wizard','answer':'false'});job=self.wait_job();self.assertEqual(job['state'],'success');self.assertTrue(job['result']['done'])
        self.assertIn('[GoogleDrive]',self.config.read_text());self.assertIsNone(self.manager.remote_wizard)
        self.assertNotIn('fixture-only-access',json.dumps(job));self.assertNotIn('fixture-only-refresh',json.dumps(job))

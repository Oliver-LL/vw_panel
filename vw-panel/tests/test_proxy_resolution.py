import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'web'))
from proxy_environment import host_proxies, recommendation, log_sources
from manager import Manager, write_private

NPM={'id':'a'*12,'name':'npm-prod','kind':'Nginx Proxy Manager','source':'docker','running':True,'networks':['npm_default']}
HOST={'id':'host:nginx','name':'nginx','kind':'Nginx','source':'host','running':True,'networks':[],'ports':[80,443]}

class ProxyResolutionTests(unittest.TestCase):
    def test_host_listener_discovery_does_not_guess_from_occupied_ports(self):
        output='LISTEN 0 511 0.0.0.0:80 0.0.0.0:* users:(("nginx",pid=123,fd=6))\nLISTEN 0 128 *:443 *:* users:(("python3",pid=2,fd=4))'
        with patch('proxy_environment.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=output)):
            proxies,complete=host_proxies()
        self.assertTrue(complete);self.assertEqual([p['id'] for p in proxies],['host:nginx']);self.assertEqual(proxies[0]['ports'],[80])
    def test_recommendations_for_existing_empty_multiple_and_unknown_environments(self):
        empty={'available':True,'complete':True,'ports_checked':True,'busy_ports':[],'proxies':[]}
        self.assertEqual(recommendation(empty)['state'],'install')
        self.assertEqual(recommendation(dict(empty,proxies=[NPM]))['target'],NPM['id'])
        self.assertEqual(recommendation(dict(empty,available=False,proxies=[HOST]))['mode'],'host_ext')
        self.assertEqual(recommendation(dict(empty,proxies=[NPM,HOST]))['state'],'choose')
        self.assertEqual(recommendation(dict(empty,busy_ports=[80]))['state'],'unknown')
        self.assertEqual(recommendation(dict(empty,complete=False))['state'],'unknown')
    def test_log_sources_follow_network_or_host_and_never_synthesize_caddy(self):
        env={'proxies':[NPM,HOST]}
        self.assertEqual(log_sources(env,{'PROXY_MODE':'docker_ext','DOCKER_NET':'npm_default'},True),[NPM])
        self.assertEqual(log_sources(env,{'PROXY_MODE':'host_ext'},True),[HOST])
        self.assertEqual(log_sources(env,{'PROXY_MODE':'docker_ext','DOCKER_NET':'npm_default','PANEL_PROXY_TARGET':'b'*12},True),[NPM])
        self.assertEqual(log_sources({'proxies':[]},{'PROXY_MODE':'caddy'},False),[])
    def test_proxy_logs_use_detected_npm_not_default_caddy(self):
        with tempfile.TemporaryDirectory() as temp:
            manager=Manager(Path(temp)/'state',Path(temp)/'memory');calls=[]
            with patch('proxy_environment.detect_environment',return_value={'proxies':[NPM]}),patch.object(manager,'emit'),patch.object(manager,'_command',side_effect=lambda args:calls.append(args)):
                manager.run_extended('proxy-logs',{})
            self.assertEqual(calls,[['docker','logs','--tail','150',NPM['id']]])
            with patch('proxy_environment.detect_environment',return_value={'proxies':[]}),patch.object(manager,'_command') as command,self.assertRaisesRegex(ValueError,'未检测到'):
                manager.run_extended('proxy-logs',{})
            command.assert_not_called()
    def test_multiple_sources_require_explicit_selection(self):
        with tempfile.TemporaryDirectory() as temp:
            manager=Manager(Path(temp)/'state',Path(temp)/'memory')
            with patch('proxy_environment.detect_environment',return_value={'proxies':[NPM,HOST]}),patch.object(manager,'emit'),patch.object(manager,'_command') as command:
                with self.assertRaisesRegex(ValueError,'多个'):manager.run_extended('proxy-logs',{})
                command.assert_not_called();manager.run_extended('proxy-logs',{'proxy_id':NPM['id']})
                command.assert_called_once_with(['docker','logs','--tail','150',NPM['id']])

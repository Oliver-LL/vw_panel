"""Installer integration test: real payload + real HTTP server, simulated systemd.
All paths are remapped to a temporary directory; no host services are changed.
"""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import signal
import socket
import tempfile
import time
import unittest
import urllib.request
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('installer_panel', ROOT/'web/server.py')
panel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(panel)


def eventually(predicate, timeout=12):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate(): return
        time.sleep(.05)
    raise AssertionError('Timeout')


class InstallerTests(unittest.TestCase):
    def test_first_install_ip_login_upgrade_and_domain_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('bin','unit','cli','systemd-present'):
                (root/name).mkdir()
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0))
                port = sock.getsockname()[1]
            source = (ROOT/'install-vw-web.sh').read_text()
            source = source.replace('[[ "$EUID" -eq 0 ]]', 'true')
            for old, new in [('/opt/vw-panel', str(root/'app')),('/var/lib/vw-panel',str(root/'state')),
                             ('/etc/systemd/system',str(root/'unit')),('/usr/local/bin/vw-panel',str(root/'cli/vw-panel')),
                             ('/run/systemd/system',str(root/'systemd-present'))]:
                source = source.replace(old,new)
            installer = root/'installer.sh'
            installer.write_text(source)
            systemctl = root/'bin/systemctl'
            systemctl.write_text('''#!/usr/bin/env python3
import json,os,pathlib,shlex,signal,subprocess,sys
root=pathlib.Path(os.environ['VW_INSTALLER_TEST_ROOT'])
pid_file=root/'service.pid'
if sys.argv[1] in ('restart','stop') and pid_file.exists():
    try:os.kill(int(pid_file.read_text()),signal.SIGTERM)
    except ProcessLookupError:pass
    # Wait for the old listener to release before restart.
    import time
    for _ in range(100):
        try:
            state=pathlib.Path('/proc/'+pid_file.read_text()+'/stat').read_text().split()[2]
            if state=='Z':break
        except FileNotFoundError:break
        time.sleep(.03)
if sys.argv[1] in ('restart','start'):
    unit=(root/'unit/vw-panel.service').read_text()
    command=next(line.split('=',1)[1] for line in unit.splitlines() if line.startswith('ExecStart='))
    with open(root/'service.log','ab') as output:
        proc=subprocess.Popen(shlex.split(command),stdout=output,stderr=output,start_new_session=True)
    pid_file.write_text(str(proc.pid))
''')
            systemctl.chmod(0o755)
            hostname = root/'bin/hostname'
            hostname.write_text('#!/bin/sh\necho 127.0.0.1\n')
            hostname.chmod(0o755)
            env = {'PATH':str(root/'bin')+':'+os.environ['PATH'], 'VW_INSTALLER_TEST_ROOT':directory}
            terminal = panel.Terminal(installer)
            try:
                with patch.dict(os.environ, env):
                    terminal.start()
                    eventually(lambda: '面板访问端口' in terminal.state()['output'])
                    terminal.send(str(port)+'\n')
                    eventually(lambda: '设置管理员密码' in terminal.state()['output'])
                    terminal.send('InstallTestPass123!\n')
                    eventually(lambda: '再次输入密码' in terminal.state()['output'])
                    terminal.send('InstallTestPass123!\n')
                    eventually(lambda: terminal.state()['exit_code'] is not None, timeout=25)
                    output = terminal.state()['output']
                    self.assertEqual(terminal.state()['exit_code'],0,output)
                    self.assertIn('安装完成', output)
                    self.assertIn('http://127.0.0.1:'+str(port),output)
                    with urllib.request.urlopen('http://127.0.0.1:'+str(port)) as response:
                        self.assertEqual(response.status,200)
                    request=urllib.request.Request('http://127.0.0.1:'+str(port)+'/api/login',
                        data=json.dumps({'password':'InstallTestPass123!'}).encode(),
                        headers={'Content-Type':'application/json','Origin':'http://127.0.0.1:'+str(port)})
                    with urllib.request.urlopen(request) as response:
                        self.assertEqual(response.status,200)
                    old_credentials=(root/'state/credentials.json').read_bytes()
                    # Upgrade is noninteractive and preserves credentials + config.
                    terminal.start()
                    eventually(lambda: terminal.state()['exit_code'] is not None, timeout=25)
                    self.assertEqual(terminal.state()['exit_code'],0,terminal.state()['output'])
                    self.assertEqual(old_credentials,(root/'state/credentials.json').read_bytes())
                    self.assertEqual(json.loads((root/'state/panel.json').read_text())['port'],port)
                    self.assertEqual(len(list((root/'app/releases').glob('20*'))),2)
                    unit=(root/'unit/vw-panel.service').read_text()
                    self.assertIn('KillMode=control-group',unit)
                    self.assertTrue((root/'cli/vw-panel').exists())
            finally:
                terminal.stop()
                pid_file=root/'service.pid'
                if pid_file.exists():
                    try:os.kill(int(pid_file.read_text()),signal.SIGTERM)
                    except ProcessLookupError:pass


if __name__=='__main__':unittest.main()

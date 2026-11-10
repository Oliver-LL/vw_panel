"""Explicit panel self-update from the configured public GitHub repository."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
REPOSITORY = 'Oliver-LL/vw_web'
CHECK_INTERVAL = 6 * 3600

def revision():
    value=json.loads(fetch('https://api.github.com/repos/'+REPOSITORY+'/git/ref/heads/main?t='+str(time.time_ns()),32768))
    reference=value.get('object') if isinstance(value,dict) else None
    commit=reference.get('sha') if isinstance(reference,dict) else None
    if not isinstance(commit,str) or not re.fullmatch(r'[a-f0-9]{40}',commit):raise ValueError('无法确认发布提交。')
    return commit

def release_base(commit):
    if not isinstance(commit,str) or not re.fullmatch(r'[a-f0-9]{40}',commit):raise ValueError('发布提交格式不正确。')
    return 'https://raw.githubusercontent.com/'+REPOSITORY+'/'+commit+'/vw-panel/'


def private_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.' + secrets.token_hex(8))
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(data, stream, ensure_ascii=False)
    temporary.replace(path)


def fetch(url, maximum):
    request = urllib.request.Request(url, headers={'User-Agent': 'Vaultwarden-Panel-Updater', 'Cache-Control': 'no-cache'})
    with urllib.request.urlopen(request, timeout=15) as response:
        body = response.read(maximum + 1)
        if len(body) > maximum:
            raise ValueError('下载内容超过限制。')
        return body


def manifest(data):
    if not isinstance(data, dict):
        raise ValueError('发布信息格式不正确。')
    for key in ('build_id', 'installer_sha256'):
        if not isinstance(data.get(key), str) or not re.fullmatch(r'[a-f0-9]{64}', data[key]):
            raise ValueError('发布信息缺少校验值。')
    if not isinstance(data.get('version'), str) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', data['version']):
        raise ValueError('发布版本格式不正确。')
    result={key: data[key] for key in ('build_id', 'version', 'installer_sha256')}
    if 'commit' in data:
        release_base(data['commit']);result['commit']=data['commit']
    return result


class Updates:
    def __init__(self, state_dir, current_dir=ROOT):
        self.state = Path(state_dir)
        self.current_dir = Path(current_dir)
        self.lock = threading.RLock()
        self.cached = None
        self.next_check = 0

    def installed(self):
        path = self.current_dir / 'version.json'
        return json.loads(path.read_text()) if path.exists() else {'version': '0.4', 'build_id': ''}

    def progress(self):
        path = self.state / 'update.json'
        value = json.loads(path.read_text()) if path.exists() else {'state': 'idle'}
        if value.get('state') == 'upgrading' and time.time() - value.get('started', 0) > 1800:
            value.update(state='failed', error='升级任务超时，请查看 journalctl -u vw-panel-update-* 后重试。')
            private_json(path, value)
        return {key: value[key] for key in ('state', 'started', 'finished', 'error', 'target_version') if key in value}

    def busy(self):
        return self.progress()['state'] == 'upgrading'

    def check(self, force=False):
        with self.lock:
            now = time.time()
            installed = self.installed()
            if force or self.cached is None or now >= self.next_check:
                try:
                    commit=revision()
                    latest = manifest(json.loads(fetch(release_base(commit)+'release.json', 32768)))
                    latest['commit']=commit
                    self.cached = {'latest': latest, 'checked_at': now, 'error': ''}
                    self.next_check = now + CHECK_INTERVAL
                except (OSError, ValueError, TimeoutError):
                    self.cached = {'latest': None, 'checked_at': now, 'error': '暂时无法检查 GitHub 更新，请稍后重试。'}
                    self.next_check = now + 900
            latest = self.cached['latest']
            return {'installed': installed, 'latest': latest, 'available': bool(latest and latest['build_id'] != installed.get('build_id')),
                    'checked_at': self.cached['checked_at'], 'error': self.cached['error'], 'progress': self.progress(),
                    'repository': 'https://github.com/' + REPOSITORY}

    def start(self, build_id):
        with self.lock:
            if self.busy():
                raise ValueError('面板正在升级，请等待完成。')
            checked = self.check(force=True)
            if checked['error']:
                raise ValueError(checked['error'])
            if not checked['available']:
                raise ValueError('当前已是仓库中的最新构建。')
            latest = checked['latest']
            if build_id != latest['build_id']:
                raise ValueError('仓库版本已变化，请重新检查后升级。')
            self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Acknowledgement is returned only after systemd accepts a separate worker.
            guards = []
            try:
                for name in ('install.lock', 'operation.lock'):
                    guard = (self.state / name).open('a'); guards.append(guard)
                    try:fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:raise ValueError('安装器或部署任务正在运行，请完成后再升级。')
                token = secrets.token_hex(12)
                request = dict(latest, state='upgrading', token=token, started=time.time(), target_version=latest['version'])
                private_json(self.state / 'update.json', request)
                worker = str(ROOT / 'web/updater.py')
                result = subprocess.run(['systemd-run', '--unit=vw-panel-update-' + token, '--collect',
                    '--property=Type=exec', sys.executable, worker, 'apply', '--state-dir', str(self.state), '--request', token],
                    capture_output=True, text=True, timeout=20)
                if result.returncode:
                    raise OSError('systemd did not accept updater')
            except (OSError, subprocess.SubprocessError):
                request = {'state': 'failed', 'error': '无法启动升级任务，请检查 systemd-run 和面板服务日志。'}
                private_json(self.state / 'update.json', request)
                raise ValueError(request['error'])
            finally:
                for guard in guards:guard.close()
            return self.progress()

    def apply(self, token):
        path = self.state / 'update.json'
        value = json.loads(path.read_text())
        if value.get('token') != token or value.get('state') != 'upgrading':
            raise ValueError('升级请求不存在或已失效。')
        expected = manifest(value)
        folder = self.state / ('update-' + token)
        try:
            folder.mkdir(mode=0o700)
            if self.installed().get('build_id') == expected['build_id']:
                value.update(state='success', finished=time.time());private_json(path, value);return
            commit=expected.get('commit')
            if not commit:
                # Safely resolve an older request without ever mixing moving branch URLs.
                commit=revision()
                pinned=manifest(json.loads(fetch(release_base(commit)+'release.json',32768)))
                if any(pinned[key]!=expected[key] for key in ('build_id','version','installer_sha256')):
                    raise ValueError('发布版本已变化，尚未执行安装。请重新检查版本后重试。')
            body = fetch(release_base(commit)+'install-vw-web.sh', 16 * 1024 * 1024)
            if hashlib.sha256(body).hexdigest() != expected['installer_sha256']:
                raise ValueError('下载校验失败，尚未执行安装。请重新检查版本后重试。')
            installer = folder / 'install.sh'
            installer.write_bytes(body);installer.chmod(0o700)
            log = self.state / 'update.log'
            fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'wb') as output:
                result = subprocess.run(['bash', str(installer), '--upgrade'], stdin=subprocess.DEVNULL,
                                        stdout=output, stderr=output, timeout=900)
            if result.returncode:
                raise ValueError('面板升级未完成，请查看 /var/lib/vw-panel/update.log；安装器启动检查失败时会尝试回退。')
            if self.installed().get('build_id') != expected['build_id']:
                raise ValueError('安装后的构建与目标不一致，请重新检查版本。')
            value.update(state='success', finished=time.time(), error='')
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            value.update(state='failed', finished=time.time(), error=str(error))
        finally:
            private_json(path, value)
            shutil.rmtree(folder, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('apply', 'busy', 'check', 'update'))
    parser.add_argument('--state-dir', type=Path, default=Path('/var/lib/vw-panel'))
    parser.add_argument('--request')
    args = parser.parse_args()
    current = Path('/opt/vw-panel/current') if args.command == 'apply' else ROOT
    updates = Updates(args.state_dir, current)
    if args.command == 'busy':
        if updates.busy():raise SystemExit('面板升级中，请完成后再操作。')
    elif args.command == 'apply':updates.apply(args.request)
    elif args.command == 'check':
        value = updates.check(force=True)
        if value['error']:raise SystemExit(value['error'])
        print('当前版本：'+value['installed']['version'])
        print('可升级到：'+value['latest']['version'] if value['available'] else '已是最新构建。')
    else:
        value = updates.check(force=True)
        if value['error']:raise SystemExit(value['error'])
        if not value['available']:print('已是最新构建。');return
        if input('升级面板并重启面板服务？[y/N]: ').lower()!='y':return
        try:updates.start(value['latest']['build_id'])
        except ValueError as error:raise SystemExit(str(error))
        print('升级任务已在后台启动；日志：/var/lib/vw-panel/update.log')


if __name__ == '__main__':main()

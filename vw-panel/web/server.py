#!/usr/bin/env python3
"""Authenticated, single-administrator bridge to the bundled Vaultwarden script."""
import argparse
import codecs
import collections
import fcntl
import getpass
import hashlib
import hmac
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import pty
import re
import secrets
import select
import signal
import struct
import shutil
import subprocess
import sys
import termios
import threading
import time
from urllib.parse import urlsplit, parse_qs, quote

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from manager import Manager, read_vars
from updater import Updates
from session_store import SessionStore
from proxy_environment import detect_environment, log_sources

SCRIPT = ROOT.parent / 'vw_web.sh'
ACTIONS = {str(i) for i in range(1, 19)}
PROMPT = '请选择操作 [1-18, q]: '
LIMIT = 512 * 1024
TTL = 12 * 3600


def digest(password, salt):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + secrets.token_hex(8))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


def valid_origin(value):
    p = urlsplit(value)
    if (p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password
            or p.path not in ('', '/') or p.query or p.fragment or any(c.isspace() for c in value)):
        raise ValueError('请输入不带路径的完整地址，例如 https://vw-panel.example.com')
    p.port  # Validate port syntax.
    return value.rstrip('/')


class Terminal:
    def __init__(self, script=SCRIPT, state_dir=None):
        self.script = str(script)
        self.state_dir = state_dir
        self.lock = threading.RLock()
        self.process = None
        self.fd = None
        self.text = ''
        self.base = 0
        self.generation = secrets.token_hex(8)
        self.pending = None
        self.exit_code = None

    def start(self, action=None):
        with self.lock:
            if self.fd is not None or (self.process and self.process.poll() is None):
                raise ValueError('当前控制台仍在运行，请先完成操作或结束控制台。')
            if action is not None and action not in ACTIONS:
                raise ValueError('未知功能。')
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 35, 120, 0, 0))
            env = dict(os.environ, VW_WEB_MODE='1', TERM='dumb', COLUMNS='100', PYTHONUNBUFFERED='1')
            if self.state_dir:
                env['VW_PANEL_LOCK'] = str(self.state_dir / 'operation.lock')
            try:
                proc = subprocess.Popen([sys.executable, str(ROOT / 'tty_worker.py'), self.script],
                                        stdin=slave, stdout=slave, stderr=slave,
                                        cwd=str(ROOT.parent), env=env, start_new_session=True)
            except Exception:
                os.close(master)
                raise
            finally:
                os.close(slave)
            self.fd, self.process = master, proc
            self.text, self.base, self.pending, self.exit_code = '', 0, action, None
            self.generation = secrets.token_hex(8)
            threading.Thread(target=self._reader, args=(master, proc), daemon=True).start()

    def _reader(self, fd, proc):
        decoder = codecs.getincrementaldecoder('utf-8')('replace')
        try:
            while True:
                if not select.select([fd], [], [], .5)[0]:
                    if proc.poll() is not None:
                        break
                    continue
                try:
                    chunk = os.read(fd, 8192)
                except OSError:
                    break
                if not chunk:
                    break
                with self.lock:
                    self.text += decoder.decode(chunk)
                    if len(self.text) > LIMIT:
                        excess = len(self.text) - LIMIT
                        self.text = self.text[excess:]
                        self.base += excess
                    if self.pending and PROMPT in self.text[-300:]:
                        os.write(fd, (self.pending + '\n').encode())
                        self.pending = None
        finally:
            code = proc.wait()
            with self.lock:
                os.close(fd)
                self.fd = None
                self.exit_code = code

    def state(self, cursor=0, generation=''):
        with self.lock:
            reset = generation != self.generation or not self.base <= cursor <= self.base + len(self.text)
            echo = True
            if self.fd is not None:
                try:
                    echo = bool(termios.tcgetattr(self.fd)[3] & termios.ECHO)
                except termios.error:
                    pass
            return {'output': self.text[0 if reset else cursor - self.base:],
                    'cursor': self.base + len(self.text), 'generation': self.generation,
                    'reset': reset, 'echo': echo, 'pending': self.pending,
                    'running': bool(self.process and self.process.poll() is None), 'exit_code': self.exit_code}

    def send(self, value):
        with self.lock:
            if self.fd is None or self.process.poll() is not None:
                raise ValueError('控制台尚未运行。')
            if len(value.encode()) > 4000:
                raise ValueError('单次输入不能超过 4000 字节。')
            os.write(self.fd, value.encode())

    def stop(self):
        with self.lock:
            proc = self.process
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
            except ProcessLookupError:
                pass


class Application:
    def __init__(self, state_dir, terminal=None):
        self.state_dir = Path(state_dir)
        self.config_path = self.state_dir / 'panel.json'
        self.config = json.loads(self.config_path.read_text())
        self.terminal = terminal or Terminal(state_dir=self.state_dir)
        self.session_store = SessionStore(self.state_dir, TTL)
        self.sessions = self.session_store.sessions
        self.attempts = collections.OrderedDict()
        self.lock = threading.RLock()
        self.operation_lock = threading.Lock()
        self.manager = Manager(self.state_dir)
        self.updates = Updates(self.state_dir)

    def allowed_origin(self, value):
        try:
            origin = valid_origin(value)
            p = urlsplit(origin)
            port = p.port or (443 if p.scheme == 'https' else 80)
            # Direct IP access works before a domain has been configured.
            try:
                ipaddress.ip_address(p.hostname)
                return p.scheme == 'http' and port == self.config['port']
            except ValueError:
                return origin in self.config.get('origins', []) or origin == 'http://localhost:%s' % self.config['port']
        except (ValueError, TypeError):
            return False

    def allowed_host(self, host):
        return self.allowed_origin('http://' + host) or self.allowed_origin('https://' + host)

    def login(self, password, address):
        now = time.monotonic()
        with self.lock:
            attempts = [t for t in self.attempts.get(address, []) if now - t < 300]
            if len(attempts) >= 5:
                raise ValueError('尝试次数过多，请 5 分钟后重试。')
            self.attempts[address] = attempts
            self.attempts.move_to_end(address)
            if len(self.attempts) > 1024:
                self.attempts.popitem(last=False)
            credentials = json.loads((self.state_dir / 'credentials.json').read_text())
            if not hmac.compare_digest(digest(password, credentials['salt']), credentials['hash']):
                attempts.append(now)
                return None
            self.attempts.pop(address, None)
            return self.session_store.create()

    def auth(self, cookie):
        try:
            parsed = SimpleCookie()
            parsed.load(cookie or '')
            token = parsed['vw_session'].value
        except (KeyError, CookieError):
            return None
        with self.lock:
            csrf = self.session_store.auth(token)
            return (token, csrf) if csrf else None

    def domain(self, origin):
        origin = valid_origin(origin)
        if urlsplit(origin).scheme != 'https':
            raise ValueError('域名反代地址请使用 HTTPS。')
        with self.lock:
            origins = list(self.config.get('origins', []))
            if origin not in origins:
                if len(origins) >= 10:
                    raise ValueError('最多配置 10 个域名访问地址。')
                origins.append(origin)
            config = dict(self.config, origins=origins)
            atomic_json(self.config_path, config)
            self.config = config
        return origin


def status():
    try:
        result = subprocess.run(['docker', 'ps', '-a', '--format', '{{json .}}'], capture_output=True, text=True, timeout=8)
        if result.returncode:
            return {'available': False, 'docker_installed': True, 'message': 'Docker 未运行或权限不足。', 'containers': []}
        rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        return {'available': True, 'docker_installed': True, 'containers': [
            {key: row.get(key, '') for key in ('Names', 'State', 'Status', 'Image')}
            for row in rows if row.get('Names') in ('vaultwarden', 'vaultwarden-backup', 'caddy', 'vw-panel-npm', 'vw-panel-nginx', 'vw-panel-certbot')]}
    except FileNotFoundError:
        return {'available': False, 'docker_installed': False, 'message': '尚未安装 Docker。', 'containers': []}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {'available': False, 'docker_installed': True, 'message': 'Docker 状态读取失败，请检查服务、权限或稍后刷新。', 'containers': []}


class Handler(BaseHTTPRequestHandler):
    server_version = 'VWPanel/0.6'

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):
        sys.stderr.write('%s %s %s\n' % (self.address_string(), self.command, urlsplit(self.path).path))

    def reply(self, code, data, mime='application/json; charset=utf-8', cookie=None):
        body = json.dumps(data, ensure_ascii=False).encode() if isinstance(data, dict) else data
        self.send_response(code)
        for key, value in {'Content-Type': mime, 'Content-Length': str(len(body)), 'Cache-Control': 'no-store',
                           'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY',
                           'Referrer-Policy': 'no-referrer',
                           'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"}.items():
            self.send_header(key, value)
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        try:
            if self.command != 'HEAD':
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def cookie(self, value, age=TTL):
        origin = self.headers.get('Origin', '')
        # The browser origin is checked before use; forwarded headers are not trusted.
        secure = origin.startswith('https://') or any(
            urlsplit(item).scheme == 'https' and urlsplit(item).netloc == self.headers.get('Host')
            for item in self.app.config.get('origins', []))
        return 'vw_session=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=%s%s' % (value, age, '; Secure' if secure else '')

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self.app.allowed_host(self.headers.get('Host', '')):
            return self.reply(403, {'error': '该域名尚未配置。请先使用 IP 登录，在面板设置中添加域名。'})
        path = urlsplit(self.path).path
        assets = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'), '/style.css': ('style.css', 'text/css; charset=utf-8')}
        if path in assets:
            name, mime = assets[path]
            return self.reply(200, (ROOT / 'static' / name).read_bytes(), mime)
        auth = self.app.auth(self.headers.get('Cookie'))
        if not auth:
            return self.reply(401, {'error': '请先登录。'})
        if path == '/api/session':
            return self.reply(200, {'csrf': auth[1]})
        if path == '/api/status':
            return self.reply(200, status())
        if path == '/api/storage/providers':
            try:return self.reply(200,{'providers':self.app.manager.storage_providers()})
            except ValueError as error:return self.reply(503,{'error':str(error)})
        if path == '/api/proxy/environment':
            environment=detect_environment()
            environment['log_sources']=log_sources(environment,read_vars(self.app.manager.memory),(self.app.manager.paths()[0]/'docker-compose.yml').exists())
            return self.reply(200, environment)
        if path == '/api/settings':
            return self.reply(200, {'port': self.app.config['port'], 'origins': self.app.config.get('origins', [])})
        if path == '/api/backups/download':
            try:
                query=parse_qs(urlsplit(self.path).query)
                file=self.app.manager.archive_path(query.get('file',[''])[0])
                with file.open('rb') as source:
                    self.send_response(200)
                    self.send_header('Content-Type','application/octet-stream')
                    self.send_header('Content-Length',str(file.stat().st_size))
                    self.send_header('Content-Disposition',"attachment; filename*=UTF-8''"+quote(file.name))
                    self.send_header('Cache-Control','no-store')
                    self.send_header('X-Content-Type-Options','nosniff')
                    self.end_headers()
                    if self.command!='HEAD':shutil.copyfileobj(source,self.wfile)
                return
            except (ValueError,OSError):
                return self.reply(404,{'error':'备份文件不可读取。'})
        if path == '/api/updates':
            try:
                force=parse_qs(urlsplit(self.path).query).get('force',['0'])[0]=='1'
                return self.reply(200,self.app.updates.check(force=force))
            except (OSError,ValueError):
                return self.reply(503,{'error':'暂时无法读取面板更新状态。'})
        if path == '/api/config':
            try:
                return self.reply(200, self.app.manager.config())
            except (OSError, ValueError):
                return self.reply(500, {'error': '读取部署配置失败，请检查目录权限。'})
        if path == '/api/task':
            return self.reply(200, self.app.manager.task())
        if path == '/api/terminal':
            query = parse_qs(urlsplit(self.path).query)
            try:
                return self.reply(200, self.app.terminal.state(int(query.get('cursor', ['0'])[0]), query.get('generation', [''])[0]))
            except ValueError:
                return self.reply(400, {'error': '游标格式错误。'})
        self.reply(404, {'error': '接口不存在。'})

    def do_POST(self):
        origin = self.headers.get('Origin', '')
        if not self.app.allowed_origin(origin) or urlsplit(origin).netloc != self.headers.get('Host'):
            return self.reply(403, {'error': '请求来源不匹配。请使用面板 IP 地址或已配置的域名。'})
        if urlsplit(self.path).path=='/api/backup/upload':
            auth=self.app.auth(self.headers.get('Cookie'))
            if not auth:return self.reply(401,{'error':'请先登录。'})
            if not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),auth[1]):
                return self.reply(403,{'error':'请求校验失败。'})
            temporary=None
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=2*1024**3:return self.reply(413,{'error':'备份文件大小须在 1 字节～2 GiB。'})
                name=parse_qs(urlsplit(self.path).query).get('name',[''])[0]
                if not name or Path(name).name!=name or any(ord(c)<32 for c in name) or not name.lower().endswith(('.zip','.7z')):
                    raise ValueError('请选择 zip 或 7z 备份。')
                with self.app.operation_lock, self.app.manager.exclusive():
                    if self.app.updates.busy():raise ValueError('面板正在升级，请完成后再上传。')
                    if self.app.terminal.state()['running']:raise ValueError('请先结束高级控制台。')
                    _,backup=self.app.manager.paths()
                    folder=backup/'backups'/'uploaded';folder.mkdir(parents=True,exist_ok=True)
                    if shutil.disk_usage(folder).free<size+512*1024**2:raise ValueError('剩余空间不足，无法上传备份。')
                    target=folder/(secrets.token_hex(5)+'-'+name)
                    temporary=target.with_name(target.name+'.uploading')
                    self.connection.settimeout(300)
                    fd=os.open(temporary,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
                    with os.fdopen(fd,'wb') as output:
                        remaining=size
                        while remaining:
                            chunk=self.rfile.read(min(1024*1024,remaining))
                            if not chunk:raise ValueError('文件上传不完整。')
                            output.write(chunk);remaining-=len(chunk)
                    os.replace(temporary,target)
                return self.reply(200,{'ok':True,'file':'uploaded/'+target.name})
            except (ValueError,OSError,TimeoutError) as error:
                return self.reply(409,{'error':str(error)})
            finally:
                if temporary:temporary.unlink(missing_ok=True)
        if self.headers.get_content_type() != 'application/json':
            return self.reply(415, {'error': '只接受 JSON 请求。'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 65536:
                return self.reply(413, {'error': '请求大小不合法。'})
            self.connection.settimeout(10)
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError()
        except (ValueError, TimeoutError):
            return self.reply(400, {'error': '请求格式错误。'})
        path = urlsplit(self.path).path
        if path == '/api/login':
            password = data.get('password', '')
            if not isinstance(password, str) or len(password) > 1024:
                return self.reply(400, {'error': '密码格式错误。'})
            try:
                auth = self.app.login(password, self.client_address[0])
            except ValueError as error:
                return self.reply(429, {'error': str(error)})
            if not auth:
                return self.reply(401, {'error': '密码错误。'})
            return self.reply(200, {'csrf': auth[1]}, cookie=self.cookie(auth[0]))
        auth = self.app.auth(self.headers.get('Cookie'))
        if not auth:
            return self.reply(401, {'error': '请先登录。'})
        if not hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), auth[1]):
            return self.reply(403, {'error': '请求校验失败，请刷新页面。'})
        try:
            if path == '/api/storage/wizard/cancel':
                from remote_config import cancel
                return self.reply(200,cancel(self.app.manager,data.get('job_id')))
            if path == '/api/updates/start':
                if data.get('confirmed') is not True:raise ValueError('请确认升级面板。')
                with self.app.operation_lock:
                    if self.app.updates.busy():raise ValueError('面板正在升级，请等待完成。')
                    if self.app.terminal.state()['running'] or self.app.manager.task()['state']=='running':
                        raise ValueError('请先完成当前任务或结束高级控制台，再升级面板。')
                    return self.reply(200,self.app.updates.start(data.get('build_id')))
            if path!='/api/logout' and self.app.updates.busy():
                raise ValueError('面板正在升级，暂时不能修改配置或执行任务。')
            if path == '/api/logout':
                with self.app.lock:
                    self.app.session_store.revoke(auth[0])
                return self.reply(200, {'ok': True}, cookie=self.cookie('', 0))
            if path == '/api/storage/config':
                return self.reply(200,self.app.manager.remote_config(data.get('name')))
            if path == '/api/proxy/credentials':
                return self.reply(200, self.app.manager.npm_credentials())
            if path == '/api/domain':
                if not isinstance(data.get('origin'), str):
                    raise ValueError('请输入域名访问地址。')
                return self.reply(200, {'origin': self.app.domain(data['origin'])})
            if path.startswith('/api/config/') or path == '/api/task/start':
                with self.app.operation_lock:
                    if self.app.updates.busy():raise ValueError('面板正在升级，请等待完成。')
                    if self.app.terminal.state()['running']:
                        raise ValueError('请先结束高级控制台，再使用表单或任务按钮。')
                    if path == '/api/task/start':
                        return self.reply(200, self.app.manager.start(data.get('action'), data))
                    return self.reply(200, self.app.manager.save(path.rsplit('/', 1)[-1], data))
            # Serialize start/stop/input, so a slow stop cannot terminate a new session.
            with self.app.operation_lock:
                if self.app.updates.busy():raise ValueError('面板正在升级，请等待完成。')
                if path == '/api/terminal/start':
                    action = data.get('action')
                    if action is not None and (not isinstance(action, str) or action not in ACTIONS):
                        raise ValueError('未知功能。')
                    if self.app.manager.task()['state'] == 'running':
                        raise ValueError('后台任务正在执行，请完成后再打开高级工具。')
                    self.app.terminal.start(action)
                elif path == '/api/terminal/input':
                    value = data.get('text')
                    if not isinstance(value, str):
                        raise ValueError('输入必须是文本。')
                    self.app.terminal.send(value)
                elif path == '/api/terminal/stop':
                    self.app.terminal.stop()
                else:
                    return self.reply(404, {'error': '接口不存在。'})
        except ValueError as error:
            return self.reply(409, {'error': str(error)})
        except OSError:
            return self.reply(500, {'error': '服务器操作失败，请检查面板服务日志。'})
        self.reply(200, {'ok': True})


def main():
    parser = argparse.ArgumentParser(description='Vaultwarden 网页管理面板')
    parser.add_argument('command', choices=['configure', 'serve', 'reset-password', 'domain'])
    parser.add_argument('--state-dir', type=Path, default=Path('/var/lib/vw-panel'))
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--origin')
    parser.add_argument('--bootstrap-secret-file', type=Path)
    args = parser.parse_args()
    config_path = args.state_dir / 'panel.json'
    credentials = args.state_dir / 'credentials.json'
    if args.command in ('configure', 'reset-password'):
        if args.command == 'configure' and not config_path.exists():
            if not 1 <= args.port <= 65535:
                raise SystemExit('端口不合法。')
            ipaddress.ip_address(args.host)
            atomic_json(config_path, {'port': args.port, 'host': args.host, 'origins': []})
        if args.command == 'reset-password' or not credentials.exists():
            password = getpass.getpass('设置管理员密码（至少 12 个字符，回车随机生成）: ')
            generated = not password
            if generated:
                password = secrets.token_urlsafe(18)
            elif len(password) < 12 or password != getpass.getpass('再次输入密码: '):
                raise SystemExit('密码太短或两次输入不一致。')
            salt = secrets.token_hex(16)
            atomic_json(credentials, {'salt': salt, 'hash': digest(password, salt)})
            if args.bootstrap_secret_file:
                from manager import write_private
                write_private(args.bootstrap_secret_file, password)
            elif generated or args.command == 'reset-password':
                print('管理员密码：' + password)
            print('密码已保存。')
        return
    if not config_path.exists() or not credentials.exists():
        raise SystemExit('请先运行安装器完成初始化。')
    app = Application(args.state_dir)
    if args.command == 'domain':
        try:
            print('已允许域名访问：' + app.domain(args.origin))
        except (ValueError, TypeError) as error:
            raise SystemExit(str(error))
        return
    lock = (args.state_dir / 'server.lock').open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('面板已在运行。')
    httpd = ThreadingHTTPServer((app.config['host'], app.config['port']), Handler)
    httpd.daemon_threads = True
    httpd.app = app
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=httpd.shutdown, daemon=True).start())
    print('VW Panel listening on %s:%s' % httpd.server_address, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.terminal.stop()
        httpd.server_close()
        lock.close()


if __name__ == '__main__':
    main()

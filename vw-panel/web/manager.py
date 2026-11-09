"""Form based configuration and bounded, non-interactive Docker tasks."""
import base64
import configparser
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import threading
import time
import sqlite3
import tarfile
from urllib.parse import urlsplit


def read_vars(path):
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if '=' not in line or line.lstrip().startswith('#'):
                continue
            key, value = line.split('=', 1)
            if value.startswith("'") and value.endswith("'"):
                value = value[1:-1].replace("\\'", "'").replace('\\\\', '\\')
            elif value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            values[key] = value
    return values


def write_private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + secrets.token_hex(6))
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def update_vars(path, updates):
    # Preserve comments and every unknown setting, including upstream secrets.
    old = path.read_text().splitlines() if path.exists() else []
    result = [line for line in old if line.split('=', 1)[0] not in updates]
    for key, value in updates.items():
        value = str(value).replace('\\', '\\\\').replace("'", "\\'")
        result.append(key + "='" + value + "'")
    if path.exists():
        write_private(path.with_name(path.name + '.panel-backup'), path.read_text())
    write_private(path, '\n'.join(result) + '\n')


def text(value, label, maximum=512):
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError(label + '格式不正确。')
    return value.strip()


def secret_text(value, label, maximum=1024):
    text(value, label, maximum)
    if not value.strip():
        raise ValueError(label + '不能仅包含空白字符。')
    return value


def number(value, label, low, high):
    try:
        value = int(value)
    except (ValueError, TypeError):
        raise ValueError(label + '必须是整数。')
    if not low <= value <= high:
        raise ValueError('%s必须在 %s～%s 之间。' % (label, low, high))
    return value


from operations import OperationsMixin, EXTRA_TASKS
from proxy_environment import existing_network, ensure_caddy_available
from managed_proxy import ManagedProxyMixin, ensure_proxy_ports


class Manager(ManagedProxyMixin, OperationsMixin):
    def __init__(self, state_dir, memory=None):
        self.state = Path(state_dir)
        self.memory = memory or Path.home() / '.vw_deploy.conf'
        self.job = {'id': '', 'state': 'idle', 'title': '暂无任务', 'output': '', 'error': ''}
        self.mutex = threading.RLock()
        self.logfile = None

    @contextlib.contextmanager
    def exclusive(self):
        self.state.mkdir(parents=True, exist_ok=True)
        with (self.state / 'operation.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('已有任务或高级控制台正在运行，请结束后再操作。')
            yield

    def revision(self, scope):
        app, backup = self.paths()
        folder = app if scope == 'core' else backup
        if not (folder / 'docker-compose.yml').exists():
            return None
        paths = [folder / 'docker-compose.yml', folder / ('vaultwarden.env' if scope == 'core' else 'backup.env')]
        if scope == 'core':
            memory = read_vars(self.memory)
            if memory.get('PROXY_MODE') == 'caddy' and memory.get('CADDY_DIR'):
                paths.append(Path(memory['CADDY_DIR']) / 'Caddyfile')
            if memory.get('PANEL_PROXY_KIND') in ('npm', 'nginx'):
                folder = self.proxy_folder(memory)
                paths.extend(folder / name for name in ('settings.json', 'docker-compose.yml', 'desired.conf', 'credentials.json'))
        else:
            paths.append(folder / 'vw-backup-config/rclone/rclone.conf')
        digest = hashlib.sha256()
        for file in paths:
            digest.update(str(file).encode());digest.update(b'\0')
            if file.name in ('vaultwarden.env','backup.env'):
                digest.update(json.dumps(read_vars(file),sort_keys=True,ensure_ascii=False).encode())
            else:
                digest.update(file.read_bytes() if file.exists() else b'absent')
            digest.update(b'\0')
        return digest.hexdigest()

    def application_record(self):
        path = self.state / 'application.json'
        return json.loads(path.read_text()) if path.exists() else {}

    def application_state(self):
        record = self.application_record()
        result = {}
        for scope in ('core', 'backup'):
            revision = self.revision(scope)
            item = record.get(scope, {})
            if revision is None: result[scope] = 'unconfigured'
            elif item.get('applied') == revision: result[scope] = 'applied'
            elif item.get('applied') or item.get('pending'): result[scope] = 'pending'
            else: result[scope] = 'unknown'
        return result

    def note_changes(self, before):
        record = self.application_record()
        changed = False
        for scope, previous in before.items():
            revision = self.revision(scope)
            if revision != previous and revision:
                item = record.setdefault(scope, {})
                item['pending'] = revision
                changed = True
        if changed:
            write_private(self.state / 'application.json', json.dumps(record))

    def note_applied(self, scope):
        revision = self.revision(scope)
        if revision:
            record = self.application_record()
            record[scope] = {'applied': revision}
            write_private(self.state / 'application.json', json.dumps(record))

    def root(self):
        saved = read_vars(self.memory)
        value = saved.get('VW_DIR', '/opt/vaultwarden')
        # Legacy script quotes its values with double quotes. Never source this file.
        return Path(value)

    def paths(self):
        root = self.root()
        return root / 'vaultwarden', root / 'vaultwarden-backup'

    def config(self):
        app, backup = self.paths()
        core = read_vars(app / 'vaultwarden.env')
        bk = read_vars(backup / 'backup.env')
        memory = read_vars(self.memory)
        safe_keys = ('DOMAIN', 'SERVER_ADMIN_EMAIL', 'SIGNUPS_ALLOWED', 'INVITATIONS_ALLOWED',
                     'SENDS_ALLOWED', 'SMTP_HOST', 'SMTP_PORT', 'SMTP_SECURITY', 'SMTP_FROM', 'SMTP_USERNAME')
        return {'application': self.application_state(), 'directory': str(self.root()), 'configured': (app / 'docker-compose.yml').exists(),
                'backup_configured': (backup / 'docker-compose.yml').exists(),
                'mode': memory.get('PANEL_PROXY_KIND') or memory.get('PROXY_MODE', 'caddy'), 'proxy': self.public_proxy(), 'port': memory.get('EXT_PORT', '8080'),
                'network': memory.get('DOCKER_NET', 'vaultwarden_net'), 'proxy_target':memory.get('PANEL_PROXY_TARGET',''),
                'core': {key: core.get(key, '') for key in safe_keys},
                'admin_set': bool(core.get('ADMIN_TOKEN')), 'smtp_password_set': bool(core.get('SMTP_PASSWORD')),
                'backup': {key: bk.get(key, '') for key in ('CRON', 'BACKUP_KEEP_DAYS', 'TIMEZONE')},
                'extras': self.extended_config(), 'zip_password_set': bool(bk.get('ZIP_PASSWORD')), 'remote': self.remote_info(), 'files': self.files()}

    def remote_info(self):
        _, backup = self.paths()
        env = read_vars(backup / 'backup.env')
        parser = configparser.ConfigParser(interpolation=None)
        path = backup / 'vw-backup-config/rclone/rclone.conf'
        try:
            parser.read(path)
            name = env.get('RCLONE_REMOTE_NAME', '')
            section = parser[name] if name in parser else {}
            return {'name': name, 'type': section.get('type', ''),
                    'url': section.get('url', ''), 'user': section.get('user', ''),
                    'directory': env.get('RCLONE_REMOTE_DIR', '/VaultBackup'),
                    'password_set': bool(section.get('pass'))}
        except configparser.Error:
            return {'name': '', 'type': 'unknown'}

    def files(self):
        _, backup = self.paths()
        folder = backup / 'backups'
        if not folder.exists():
            return []
        files = sorted((p for p in folder.rglob('*') if p.is_file() and not p.is_symlink()),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:40]
        return [{'name': str(p.relative_to(folder)), 'size': p.stat().st_size, 'modified': p.stat().st_mtime}
                for p in files]

    def save(self, section, data):
        with self.exclusive():
            before = {scope: self.revision(scope) for scope in ('core', 'backup')}
            try:
                result = self._save(section, data)
            finally:
                self.note_changes(before)
            scope = 'core' if section in ('core','mail','advanced','push') else 'backup'
            state = self.application_state()[scope]
            if scope=='core' and not read_vars(self.paths()[0]/'vaultwarden.env').get('ADMIN_TOKEN'):
                result['message']='配置已保存。设置管理口令后即可部署密码库。'
            elif state=='pending':
                result['message']=('密码库' if scope=='core' else '备份')+'设置已保存，等待应用到服务。'
            elif state=='applied':
                result['message']='设置已保存，内容未改变，无需重新应用。'
            else:
                result['message']='设置已保存。现有部署尚无应用记录，可按需重新应用。'
            return result

    def _save(self, section, data):
        app, backup = self.paths()
        if section == 'core':
            return self.save_core(data, app)
        if section == 'mail':
            if not (app / 'vaultwarden.env').exists():
                raise ValueError('请先保存密码库配置。')
            keys = ('SMTP_HOST', 'SMTP_PORT', 'SMTP_SECURITY', 'SMTP_FROM', 'SMTP_USERNAME')
            updates = {key: text(data.get(key, ''), key) for key in keys}
            if updates['SMTP_PORT']:
                number(updates['SMTP_PORT'], '邮件端口', 1, 65535)
            if updates['SMTP_SECURITY'] not in ('starttls', 'force_tls', 'off'):
                raise ValueError('请选择邮件加密方式。')
            if data.get('SMTP_PASSWORD'):
                updates['SMTP_PASSWORD'] = secret_text(data['SMTP_PASSWORD'], '邮件密码')
            update_vars(app / 'vaultwarden.env', updates)
        elif section in ('advanced','push','notifications','targets'):
            self.save_extended(section,data)
        elif section == 'backup':
            if not (app / 'docker-compose.yml').exists():
                raise ValueError('请先配置密码库。')
            cron = data.get('CRON', '')
            if not isinstance(cron,str) or len(cron.split())!=5 or not re.fullmatch(r'[0-9* /,\-]+',cron):
                raise ValueError('请输入标准五段 CRON 表达式。')
            bounds=((0,59),(0,23),(1,31),(1,12),(0,7))
            for part,(low,high) in zip(cron.split(),bounds):
                for item in part.split(','):
                    base,sep,step=item.partition('/')
                    if sep and (not step.isdigit() or not 1<=int(step)<=high+1):raise ValueError('CRON 步长无效。')
                    if base!='*':
                        values=base.split('-')
                        if len(values)>2 or any(not v.isdigit() or not low<=int(v)<=high for v in values):raise ValueError('CRON 数值超出范围。')
                        if len(values)==2 and int(values[0])>int(values[1]):raise ValueError('CRON 范围无效。')
            timezone = text(data.get('TIMEZONE', 'Asia/Shanghai'), '时区')
            if not re.fullmatch(r'[A-Za-z_]+(?:/[A-Za-z0-9_+\-]+)+|UTC', timezone):
                raise ValueError('时区格式不正确。')
            updates = {'CRON': cron, 'TIMEZONE': timezone,
                       'BACKUP_KEEP_DAYS': number(data.get('BACKUP_KEEP_DAYS'), '保留天数', 0, 3650)}
            old = read_vars(backup / 'backup.env')
            if data.get('ZIP_PASSWORD'):
                password = secret_text(data['ZIP_PASSWORD'], '备份密码')
                if len(password) < 12:
                    raise ValueError('备份密码至少 12 个字符。')
                updates['ZIP_PASSWORD'] = password
            elif not old.get('ZIP_PASSWORD'):
                raise ValueError('首次配置请设置备份加密密码。')
            if not (backup / 'docker-compose.yml').exists():
                updates.update(RCLONE_REMOTE_NAME='BitwardenBackup', RCLONE_REMOTE_DIR='/bitwarden/backup/archive',
                               BACKUP_FILE_SUFFIX='%Y%m%d-%H%M%S')
                for name in ('backups', 'vw-backup-config/rclone'):
                    (backup / name).mkdir(parents=True, exist_ok=True)
                write_private(backup / 'vw-backup-config/rclone/rclone.conf', '[BitwardenBackup]\ntype = local\n')
                compose = {'services': {'backup': {'image': 'ttionya/vaultwarden-backup:latest',
                    'container_name': 'vaultwarden-backup', 'restart': 'unless-stopped',
                    'env_file': ['./backup.env'], 'volumes': ['../vaultwarden/data:/bitwarden/data:ro',
                    './backups:/bitwarden/backup', './vw-backup-config:/config']}}}
                write_private(backup / 'docker-compose.yml', json.dumps(compose, indent=2))
            update_vars(backup / 'backup.env', updates)
        else:
            raise ValueError('未知配置页面。')
        return {'ok': True, 'message': '已保存。点击“应用配置”后生效。'}


    def save_core(self, data, app):
        current = self.config()
        domain = text(data.get('DOMAIN', ''), '密码库地址')
        p = urlsplit(domain)
        if p.scheme != 'https' or not p.hostname or p.path not in ('', '/') or p.query or p.fragment or p.username or p.password:
            raise ValueError('请输入完整的 HTTPS 密码库域名，不带路径。')
        if not re.fullmatch(r'[A-Za-z0-9.-]+', p.hostname):
            raise ValueError('域名格式不正确。')
        p.port
        password = data.get('ADMIN_TOKEN', '')
        updates = {'DOMAIN': domain.rstrip('/'), 'SERVER_ADMIN_EMAIL': text(data.get('SERVER_ADMIN_EMAIL', ''), '邮箱')}
        for key in ('SIGNUPS_ALLOWED', 'INVITATIONS_ALLOWED', 'SENDS_ALLOWED'):
            if not isinstance(data.get(key), bool):
                raise ValueError('开关值不正确。')
            updates[key] = str(data[key]).lower()
        # Token hashing is a separate job; never store a new plaintext admin token.
        if password:
            raise ValueError('请通过“设置管理口令”单独提交密码。')
        memory = read_vars(self.memory)
        if not current['configured']:
            directory = text(data.get('directory', '/opt/vaultwarden'), '安装目录')
            if not re.fullmatch(r'/[A-Za-z0-9_./-]+', directory) or '..' in Path(directory).parts or len(Path(directory).parts) < 3:
                raise ValueError('请使用 /opt/vaultwarden 这样的独立绝对目录。')
            app = Path(directory) / 'vaultwarden'
            if app.exists() and any(app.iterdir()):
                raise ValueError('目标已有文件，请先确认原部署目录，避免覆盖已有数据。')
            mode = data.get('mode', 'caddy')
            if mode not in ('caddy', 'npm', 'nginx', 'host_ext', 'docker_ext'):
                raise ValueError('请选择访问方式。')
            network = text(data.get('network', 'vaultwarden_net'), '网络名')
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', network):
                raise ValueError('Docker 网络名不正确。')
            if mode == 'docker_ext':
                existing_network(network)
            elif mode == 'caddy':
                ensure_caddy_available()
            if mode in ('caddy', 'npm', 'nginx') and p.port not in (None, 443):
                raise ValueError('自动安装的反代使用 HTTPS 443 端口，请填写不带自定义端口的域名。')
            proxy_plan = self.prepare_proxy(mode, Path(directory), p.hostname, network, updates['SERVER_ADMIN_EMAIL'], data, existing=memory.get('PANEL_PROXY_KIND')==mode and memory.get('PANEL_PROXY_DIR')==str(Path(directory)/mode)) if mode in ('npm', 'nginx') else None
            port = number(data.get('port', 8080), '密码库端口', 1024, 65535)
            target=data.get('PROXY_TARGET','')
            if not isinstance(target,str) or target and not re.fullmatch(r'(?:[a-f0-9]{12,64}|host:(?:nginx|caddy|traefik|haproxy|apache2|httpd))',target):
                raise ValueError('所选反代信息无效，请重新检测。')
            if target:
                from proxy_environment import detect_environment
                choices=detect_environment().get('proxies',[])
                match=next((item for item in choices if item['id']==target and item.get('running')),None)
                if not match or mode not in ('host_ext','docker_ext') or (mode=='host_ext' and match.get('source')!='host') or (mode=='docker_ext' and network not in match.get('networks',[])):
                    raise ValueError('所选反代或网络已变化，请重新检测后保存。')
            memory['PANEL_PROXY_TARGET']=target
            memory.update(VW_DIR=directory, PROXY_MODE='docker_ext' if mode in ('npm', 'nginx') else mode, DOCKER_NET=network, EXT_PORT=str(port),
                          SKIP_CADDY='false' if mode == 'caddy' else 'true')
            service = {'image': 'vaultwarden/server:latest', 'container_name': 'vaultwarden',
                       'restart': 'unless-stopped', 'env_file': ['./vaultwarden.env'],
                       'volumes': ['./data:/data'], 'networks': [network]}
            if mode == 'host_ext':
                service['ports'] = ['127.0.0.1:%s:80' % port]
            compose = {'services': {'vaultwarden': service}, 'networks': {network: {'external': True}}}
            if mode == 'caddy':
                caddy = Path(directory) / 'caddy'
                if caddy.exists() and any(caddy.iterdir()):
                    raise ValueError('Caddy 目录已存在，请先确认配置。')
            (app / 'data').mkdir(parents=True, exist_ok=True)
            write_private(app / 'docker-compose.yml', json.dumps(compose, indent=2))
            if mode == 'caddy':
                caddy = Path(directory) / 'caddy'
                if caddy.exists() and any(caddy.iterdir()):
                    raise ValueError('Caddy 目录已存在，请先确认配置。')
                proxy = {'services': {'caddy': {'image': 'caddy:2', 'container_name': 'caddy',
                    'restart': 'unless-stopped', 'ports': ['80:80', '443:443'], 'networks': [network],
                    'volumes': ['./Caddyfile:/etc/caddy/Caddyfile:ro', './data:/data', './config:/config']}},
                    'networks': {network: {'external': True}}}
                write_private(caddy / 'docker-compose.yml', json.dumps(proxy, indent=2))
                write_private(caddy / 'Caddyfile', p.netloc + ' {\n reverse_proxy vaultwarden:80\n}\n')
                memory.update(CADDY_DIR=str(caddy), CADDY_EXISTS='false')
            if proxy_plan:
                self.write_proxy_plan(proxy_plan)
                memory.update(PANEL_PROXY_KIND=mode, PANEL_PROXY_DIR=str(proxy_plan[0]))
            else:
                memory.pop('PANEL_PROXY_KIND', None);memory.pop('PANEL_PROXY_DIR', None)
            # Script memory uses double quoted literals; only validated topology values enter it.
            write_private(self.memory, '\n'.join('%s="%s"' % (k, v) for k, v in memory.items()) + '\n')
            updates.update(TZ='Asia/Shanghai', WEB_VAULT_ENABLED='true', ENABLE_DB_WAL='true')
        elif current['mode'] == 'caddy' and current['core'].get('DOMAIN') != domain.rstrip('/'):
            caddy_file=Path(memory.get('CADDY_DIR','/nonexistent'))/'Caddyfile'
            if not caddy_file.exists():raise ValueError('没有找到已管理的 Caddy 配置文件。')
            content=caddy_file.read_text()
            old_host=urlsplit(current['core'].get('DOMAIN','')).netloc
            begin='# BEGIN VW_MANAGED '+old_host
            end='# END VW_MANAGED '+old_host
            if begin in content and end in content:
                replacement='# BEGIN VW_MANAGED '+p.netloc+'\n'+p.netloc+' {\n reverse_proxy vaultwarden:80\n}\n# END VW_MANAGED '+p.netloc
                content=content[:content.index(begin)]+replacement+content[content.index(end)+len(end):]
            elif content.count('reverse_proxy')==1 and content.count('{')==1 and old_host in content:
                content=content.replace(old_host,p.netloc,1)
            else:raise ValueError('未识别到独立或带管理标记的站点配置，已保留原文件。')
            write_private(caddy_file.with_name('Caddyfile.panel-backup'),caddy_file.read_text())
            write_private(caddy_file,content)
        if current['configured'] and current['mode'] in ('npm', 'nginx'):
            if p.port not in (None, 443):raise ValueError('自动安装的反代使用 HTTPS 443 端口。')
            plan = self.prepare_proxy(current['mode'], self.root(), p.hostname, current['network'], updates['SERVER_ADMIN_EMAIL'], data, existing=True)
            self.write_proxy_plan(plan)
        update_vars(app / 'vaultwarden.env', updates)
        return {'ok': True, 'message': '密码库配置已保存。首次部署请设置管理口令，再点击“部署 / 应用配置”。'}

    def task(self):
        with self.mutex:
            result = dict(self.job)
            if self.logfile:
                self.logfile.flush()
                with open(self.logfile.name, 'rb') as stream:
                    stream.seek(max(0, os.fstat(stream.fileno()).st_size - 32000))
                    result['output'] = stream.read().decode('utf-8', 'replace')
            return result

    def start(self, action, data):
        titles = {'deploy': '部署 / 应用配置', 'stop': '停止密码库', 'upgrade': '升级密码库',
                  'backup-start': '应用备份配置', 'backup-stop': '停止自动备份', 'backup-now': '立即备份',
                  'admin-token': '加密管理口令', 'logs': '读取服务日志', 'backup-logs': '读取备份日志', 'install-docker': '安装 Docker', 'remote-save': '连接 WebDAV 备份存储'}
        titles.update(EXTRA_TASKS)
        if not isinstance(action, str) or action not in titles:
            raise ValueError('未知操作。')
        if action in ('stop', 'upgrade', 'backup-stop', 'install-docker') and data.get('confirmed') is not True:
            raise ValueError('请确认此操作。')
        if action in EXTRA_TASKS:
            password=self.prepare_extended(action,data)
        elif action == 'admin-token':
            password = secret_text(data.get('password', ''), '管理口令')
            if len(password) < 12:
                raise ValueError('管理口令至少 12 个字符。')
        elif action == 'remote-save':
            url = text(data.get('url', ''), 'WebDAV 地址')
            parsed = urlsplit(url)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.query:
                raise ValueError('请输入完整的 HTTPS WebDAV 地址。')
            directory = text(data.get('directory', '/VaultBackup'), '远程目录')
            if not directory.startswith('/') or '..' in Path(directory).parts:
                raise ValueError('请输入 /VaultBackup 这样的远程目录。')
            password = {'url': url, 'directory': directory,
                        'user': text(data.get('user', ''), 'WebDAV 用户名'),
                        'password': secret_text(data['password'], 'WebDAV 密码') if data.get('password') else ''}
        else:
            password = None
        # Acquire the cross-process lock before returning, avoiding a start/start race.
        guard = self.exclusive()
        guard.__enter__()
        with self.mutex:
            if self.logfile:
                self.logfile.close()
                Path(self.logfile.name).unlink(missing_ok=True)
            self.logfile = tempfile.NamedTemporaryFile(mode='w+b', delete=False, dir=self.state, prefix='panel-task-', suffix='.log')
            self.job = {'id': secrets.token_hex(8), 'state': 'running', 'title': titles[action],
                        'output': '', 'error': '', 'action': action, 'started': time.time()}
        threading.Thread(target=self._run, args=(action, password, guard), daemon=True).start()
        return self.task()

    def _command(self, args, input_text=None, secret=False):
        if not secret:
            with self.mutex:
                self.logfile.write(('正在执行：' + ' '.join(args[:3]) + '\n').encode())
                self.logfile.flush()
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            try:
                result = subprocess.run(args, input=input_text.encode() if input_text is not None else None,
                                        stdout=output, stderr=errors if secret else output, timeout=900)
            except subprocess.TimeoutExpired:
                raise ValueError('操作超时，请检查容器日志后再重试。')
            output.seek(0)
            raw = output.read(4*1024*1024 if secret else 64000).decode('utf-8', 'replace')
        if not secret:
            # Docker output may contain configuration secrets. Redact known env values.
            app, backup = self.paths()
            for path in (app / 'vaultwarden.env', backup / 'backup.env'):
                for key, value in read_vars(path).items():
                    if any(word in key for word in ('TOKEN', 'PASSWORD', 'SECRET', 'KEY', 'SMTP_VARIABLES')) and value:
                        raw = raw.replace(value, '[已隐藏]').replace(value.replace('$$', '$'), '[已隐藏]')
            if read_vars(self.memory).get('PANEL_PROXY_KIND') == 'npm':
                password = self.npm_credentials()['password']
                if password:raw = raw.replace(password, '[已隐藏]')
            raw = re.sub(r'(Creating a new user: [^\r\n]* with password: )[^\r\n]*', r'\1[已隐藏]', raw, flags=re.IGNORECASE)
            with self.mutex:
                self.logfile.write(raw.encode())
                self.logfile.flush()
        if result.returncode:
            raise ValueError('命令执行失败，请查看任务详情。' if not secret else '操作失败，请检查连接与配置。凭据未写入日志。')
        return raw

    def _run(self, action, password, guard):
        before = {}; succeeded = False
        try:
            before = {scope: self.revision(scope) for scope in ('core', 'backup')}
            app, backup = self.paths()
            memory = read_vars(self.memory)
            if action in EXTRA_TASKS:
                self.run_extended(action,password)
                if action=='upgrade-backup':self.note_applied('backup')
                succeeded=True
                return
            if not (app / 'docker-compose.yml').exists() and action not in ('logs', 'backup-logs', 'install-docker'):
                raise ValueError('请先保存密码库配置。')
            compose = lambda folder, *args: ['docker', 'compose', '--project-directory', str(folder), '-f', str(folder / 'docker-compose.yml'), *args]
            if action == 'install-docker':
                with tempfile.NamedTemporaryFile() as script:
                    self._command(['curl', '-fsSL', '--connect-timeout', '15', '--max-time', '120', 'https://get.docker.com', '-o', script.name])
                    self._command(['sh', script.name])
                self._command(['docker', 'compose', 'version'])
            elif action == 'admin-token':
                salt = base64.b64encode(secrets.token_bytes(32)).decode()
                raw = self._command(['docker', 'run', '--rm', '-i', 'alpine:3.20', 'sh', '-c',
                    'apk add --no-cache argon2 >/dev/null 2>&1 && argon2 "$1" -e -id -m 16 -t 3 -p 4',
                    'hash', salt], password, secret=True)
                match = re.search(r'\$argon2id\$[^\s\'"`]+', raw)
                if not match:
                    raise ValueError('未获得有效加密结果，原口令未修改。')
                update_vars(app / 'vaultwarden.env', {'ADMIN_TOKEN': match.group()})
            elif action == 'remote-save':
                if not (backup / 'backup.env').exists():
                    raise ValueError('请先保存备份计划和加密密码。')
                config = backup / 'vw-backup-config/rclone/rclone.conf'
                parser = configparser.ConfigParser(interpolation=None)
                parser.read(config)
                previous = dict(parser['PanelWebDAV']) if 'PanelWebDAV' in parser else {}
                if password['password']:
                    obscured = self._command(['docker', 'run', '--rm', '-i', 'ttionya/vaultwarden-backup:latest',
                        'rclone', 'obscure', '-'], password['password'] + '\n', secret=True).strip()
                    if not re.fullmatch(r'[A-Za-z0-9_+/=-]+', obscured):
                        raise ValueError('WebDAV 密码处理失败，原配置未修改。')
                else:
                    obscured = previous.get('pass', '')
                    if not obscured:
                        raise ValueError('首次连接请填写 WebDAV 密码。')
                block = '[PanelWebDAV]\ntype = webdav\nvendor = other\nurl = %s\nuser = %s\npass = %s\n' % (password['url'], password['user'], obscured)
                original = config.read_text() if config.exists() else ''
                candidate = re.sub(r'(?ms)^\[PanelWebDAV\]\s*\n.*?(?=^\[|\Z)', '', original) + '\n' + block
                with tempfile.TemporaryDirectory(dir=self.state) as directory:
                    trial = Path(directory) / 'rclone.conf'
                    write_private(trial, candidate)
                    base = ['docker', 'run', '--rm', '-v', str(trial) + ':/config/rclone/rclone.conf:ro', '--entrypoint','rclone',
                            'ttionya/vaultwarden-backup:latest']
                    target = 'PanelWebDAV:' + password['directory']
                    try:
                        self._command(base + ['mkdir', target], secret=True)
                        self._command(base + ['lsd', target], secret=True)
                    except ValueError:
                        raise ValueError('无法连接 WebDAV，请检查地址、账号和目录权限。原目标未修改。')
                if config.exists():
                    write_private(config.with_name('rclone.conf.panel-backup'), original)
                write_private(config, candidate)
                update_vars(backup / 'backup.env', {'RCLONE_REMOTE_NAME': 'PanelWebDAV', 'RCLONE_REMOTE_DIR': password['directory']})
                self.logfile.write(('WebDAV 连接验证成功，配置未变化，无需重新应用。\n' if before.get('backup')==self.revision('backup') else 'WebDAV 连接验证成功，配置已保存。应用备份配置后加载新目标。\n').encode())
            elif action in ('logs', 'backup-logs'):
                self._command(['docker', 'logs', '--tail', '150', 'vaultwarden' if action == 'logs' else 'vaultwarden-backup'])
            elif action == 'stop':
                self._command(['docker', 'stop', 'vaultwarden'])
            elif action.startswith('backup-'):
                if not (backup / 'docker-compose.yml').exists():
                    raise ValueError('请先保存备份配置。')
                if action == 'backup-now':
                    self._command(['docker', 'exec', 'vaultwarden-backup', '/app/backup.sh'])
                else:
                    self._command(compose(backup, *(['up', '-d', '--force-recreate'] if action == 'backup-start' else ['stop'])))
                    if action == 'backup-start':
                        result = self._command(['docker', 'inspect', '--format', '{{.State.Running}}', 'vaultwarden-backup'])
                        if result.strip() != 'true':
                            raise ValueError('备份容器未保持运行，请查看备份日志。')
            else:
                env = read_vars(app / 'vaultwarden.env')
                if not env.get('ADMIN_TOKEN'):
                    raise ValueError('请先在密码库配置页设置管理口令。')
                self._command(['docker', 'compose', 'version'])
                network = memory.get('DOCKER_NET')
                if memory.get('PROXY_MODE') == 'docker_ext' and not memory.get('PANEL_PROXY_KIND'):
                    existing_network(network)
                if memory.get('PANEL_PROXY_KIND') in ('npm', 'nginx'):
                    settings = self.proxy_settings()
                    ensure_proxy_ports(settings['kind'], self.proxy_folder(), settings.get('admin_port'))
                if memory.get('PROXY_MODE') == 'caddy' and memory.get('CADDY_EXISTS') == 'false':
                    ensure_caddy_available(Path(memory['CADDY_DIR']) / 'Caddyfile')
                if network:
                    result = subprocess.run(['docker', 'network', 'inspect', network], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                    if result.returncode:
                        if memory.get('PROXY_MODE') == 'docker_ext' and not memory.get('PANEL_PROXY_KIND'):
                            raise ValueError('反代网络已不存在，请恢复原网络后重新部署。')
                        self._command(['docker', 'network', 'create', network])
                if action == 'upgrade':
                    self._command(compose(app, 'pull'))
                self._command(compose(app, 'up', '-d'))
                if memory.get('PROXY_MODE') == 'caddy' and memory.get('CADDY_EXISTS') == 'false':
                    self._command(compose(Path(memory['CADDY_DIR']), 'up', '-d'))
                if memory.get('PANEL_PROXY_KIND') in ('npm', 'nginx'):
                    self.deploy_managed_proxy(upgrade=action=='upgrade')
                if action=='upgrade' and (backup/'docker-compose.yml').exists():
                    self._command(compose(backup,'pull'))
                    self._command(compose(backup,'up','-d'))
                if memory.get('PROXY_MODE')=='caddy':
                    self._command(['docker','exec',memory.get('EXISTING_CADDY_ID') or 'caddy','caddy','reload','--config','/etc/caddy/Caddyfile'])
                result = self._command(['docker', 'inspect', '--format', '{{.State.Running}}', 'vaultwarden'])
                if result.strip() != 'true':
                    raise ValueError('容器未保持运行，请查看服务日志。')
            if action in ('deploy','upgrade'):self.note_applied('core')
            if action in ('backup-start','upgrade') and self.revision('backup'):self.note_applied('backup')
            succeeded=True
        except (ValueError, OSError, subprocess.SubprocessError, configparser.Error, sqlite3.Error, tarfile.TarError) as error:
            with self.mutex:
                self.job.update(state='failed', error=str(error))
        finally:
            try:
                self.note_changes(before)
                if succeeded:
                    with self.mutex:self.job['state']='success'
            except (OSError,ValueError):
                with self.mutex:self.job.update(state='failed',error='配置应用记录保存失败，请检查面板状态目录权限。')
            finally:guard.__exit__(None, None, None)


if __name__ == '__main__':
    import sys
    if len(sys.argv)==3 and sys.argv[1]=='--export-env':
        for key,value in read_vars(Path(sys.argv[2])).items():print(key+'='+value)
    elif len(sys.argv) == 3:
        print(read_vars(Path(sys.argv[1])).get(sys.argv[2], ''))

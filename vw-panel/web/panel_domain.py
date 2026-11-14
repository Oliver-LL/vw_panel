"""Create and verify a panel HTTPS route before restricting direct access."""
import http.client
import ipaddress
import json
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import time
from urllib.parse import urlsplit

from existing_npm import ExistingNPM
import panel_access
from proxy_environment import detect_environment, docker

TABLE = 'vw_panel_access'


def normalize(value):
    if not isinstance(value, str) or any(c.isspace() for c in value.strip()):
        raise ValueError('请输入面板域名，例如 panel.example.com。')
    value = value.strip()
    url = urlsplit(value if '://' in value else 'https://' + value)
    host = (url.hostname or '').rstrip('.')
    labels = host.split('.')
    if (url.scheme != 'https' or url.username is not None or url.password is not None
            or url.path not in ('', '/') or url.query or url.fragment or url.port not in (None, 443)
            or len(host) > 253 or len(labels) < 2 or not any(c.isalpha() for c in labels[-1])
            or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels)):
        raise ValueError('请输入独立的 HTTPS 域名，无需填写协议、端口或路径。')
    return host


def npm_options(environment):
    return [proxy for proxy in environment.get('proxies', [])
            if proxy['kind'] == 'Nginx Proxy Manager' and proxy.get('running')
            and proxy.get('https_published') and proxy.get('networks')]


def managed_npm(manager, proxy):
    if proxy['name'] != 'nginx-proxy-manager':
        return False
    try:
        folder = manager.proxy_folder()
        if folder is None or manager.proxy_settings().get('kind') != 'npm':
            return False
        result = docker(['inspect', '--format', '{{json .Mounts}}', proxy['id']])
        return not result.returncode and any(
            mount.get('Destination') == '/data'
            and Path(mount.get('Source', '')).resolve() == (folder / 'data').resolve()
            for mount in json.loads(result.stdout)
        )
    except (ValueError, OSError, subprocess.SubprocessError):
        return False


def network_endpoint(proxy):
    result = docker(['inspect', '--format', '{{json .NetworkSettings.Networks}}', proxy['id']])
    if result.returncode:
        raise ValueError('无法读取 NPM 容器网络，请重新检测。')
    networks = json.loads(result.stdout)
    for name in proxy['networks']:
        item = networks.get(name, {})
        try:
            address = ipaddress.IPv4Address(item.get('IPAddress', ''))
            gateway = ipaddress.IPv4Address(item.get('Gateway', ''))
        except ValueError:
            continue
        if not all(ip.is_private and not (ip.is_loopback or ip.is_link_local or ip.is_unspecified)
                   for ip in (address, gateway)):
            continue
        inspected = docker(['network', 'inspect', '--format', '{{json .}}', name])
        if inspected.returncode or json.loads(inspected.stdout).get('Driver') != 'bridge':
            continue
        route = subprocess.run(['ip', '-j', '-4', 'route', 'get', str(address)],
                               capture_output=True, text=True, timeout=5, check=True)
        rows = json.loads(route.stdout)
        if not rows or rows[0].get('prefsrc') != str(gateway):
            continue
        interface = rows[0].get('dev', '')
        if re.fullmatch(r'[a-zA-Z0-9_.-]{1,15}', interface) and interface != 'lo':
            return str(address), str(gateway), interface
    raise ValueError('没有找到可安全访问面板的本机 NPM 网桥。')


def firewall(port=None, gateway=None, interface=None):
    nft = shutil.which('nft')
    if not nft:
        if port is None:
            return
        raise ValueError('服务器缺少 nft 命令，无法自动限制面板直连端口。请先安装 nftables。')
    listed = subprocess.run([nft, 'list', 'tables'], capture_output=True, text=True, timeout=5)
    if listed.returncode:
        raise ValueError('无法读取服务器防火墙，请检查 nftables 是否可用。')
    exists = 'table inet ' + TABLE in listed.stdout.splitlines()
    rules = ['delete table inet ' + TABLE] if exists else []
    if port is not None:
        port = int(port)
        if not 1024 <= port <= 65535 or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,15}', interface or ''):
            raise ValueError('面板端口或网桥配置无效。')
        gateway = str(ipaddress.IPv4Address(gateway))
        rules += [
            f'add table inet {TABLE}',
            f'add chain inet {TABLE} input {{ type filter hook input priority -5; policy accept; }}',
            f'add rule inet {TABLE} input tcp dport {port} iifname "lo" accept',
            f'add rule inet {TABLE} input tcp dport {port} iifname "{interface}" ip daddr {gateway} accept',
            f'add rule inet {TABLE} input tcp dport {port} drop',
        ]
    if rules:
        result = subprocess.run([nft, '-f', '-'], input='\n'.join(rules) + '\n',
                                capture_output=True, text=True, timeout=5)
        if result.returncode:
            raise ValueError('无法应用面板端口限制。请检查 nftables 状态。')


def verify_https(host, token, expected):
    # A fresh challenge ensures a valid certificate alone cannot be mistaken for this panel.
    for _ in range(5):
        connection = http.client.HTTPSConnection(host, timeout=5)
        try:
            connection.request('GET', '/_vw-panel-check/' + token)
            response = connection.getresponse()
            if response.status == 200 and response.read(128) == expected.encode():
                return
        except (OSError, http.client.HTTPException):
            pass
        finally:
            connection.close()
        time.sleep(1)
    raise ValueError('HTTPS 尚未连到本面板。请检查域名 A / AAAA 记录、反代的 80 / 443 端口及证书状态。')


class PanelNPM(ExistingNPM):
    def __init__(self, manager, proxy, domain, gateway, port, address, email, password):
        self.manager = manager
        self.domain = domain
        self.base = f'http://{address}:81/api/'
        self.folder = manager.state / 'panel-npm' / proxy['id'] / domain
        self.forward_host, self.forward_port = gateway, port
        if not password and managed_npm(manager, proxy):
            credentials = manager.npm_credentials()
            email, password = credentials['email'], credentials['password']
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email) or not password:
            raise ValueError('请输入现有 NPM 的管理邮箱和密码，供本次自动配置使用。')
        self.email, self.password = email, password
        self.route_label = '面板'

    def configure(self):
        try:
            self.configure_npm()
        finally:
            self.password = ''


def configure(app, data):
    host = normalize(data.get('domain'))
    origin = 'https://' + host
    manager = app.manager
    before = app.config
    previous = before.get('panel_route')
    vault, _ = manager.paths()
    from manager import read_vars
    if urlsplit(read_vars(vault / 'vaultwarden.env').get('DOMAIN', '')).hostname == host:
        raise ValueError('此域名已用于密码库，请为面板使用另一个域名。')
    try:
        socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise ValueError('域名尚未解析，请先添加指向服务器的 A 或 AAAA 记录。') from None
    environment = detect_environment()
    options = npm_options(environment)
    selected = data.get('proxy')
    proxy = next((item for item in options if item['id'] == selected), None)
    if proxy is None:
        if selected or len(options) != 1:
            if not options:
                raise ValueError('未找到可自动配置的 NPM。检测到的其他反代暂不支持自动写入。')
            raise ValueError('请选择当前用于对外访问的 NPM。')
        proxy = options[0]
    address, gateway, interface = network_endpoint(proxy)
    port = before['port']
    protected = bool(previous and previous.get('protected'))
    if protected and (gateway, interface, port) != (previous['gateway'], previous['interface'], previous['port']):
        raise ValueError('反代网络已变化，请先检测并修复反代，再修改域名。')
    if previous and previous['origin'] == origin:
        if proxy['name'] != previous['proxy']:
            raise ValueError('同一域名已绑定另一台 NPM。请选择原 NPM，或使用其他域名。')
        return repair(app, data)

    manager.emit('已检测到 NPM：' + proxy['name'])
    route = PanelNPM(manager, proxy, host, gateway, port, address, data['email'], data['password'])
    token = secrets.token_hex(24)
    proof = secrets.token_hex(24)
    app.domain_challenge = (host, token, proof)
    try:
        app.domain(origin)
        route.configure()
        manager.emit('正在验证 HTTPS 是否连接到本面板。')
        verify_https(host, token, proof)
        config = dict(app.config, panel_route={
            'origin': origin, 'gateway': gateway, 'interface': interface,
            'proxy': proxy['name'], 'port': port, 'protected': protected,
        })
        app.save_config(config)
    except Exception:
        app.save_config(before)
        raise
    finally:
        app.domain_challenge = None
    manager.emit('HTTPS 验证通过。' if protected else 'HTTPS 验证通过。通过新域名登录后，将自动关闭公网直连。')
    return {'origin': origin, 'proxy': proxy['name'], 'protected': protected}



def protect(app, request_origin):
    route = app.config.get('panel_route', {})
    if not route or request_origin != route.get('origin') or app.config['port'] != route.get('port'):
        raise ValueError('请先打开已配置的 HTTPS 面板域名并登录。')
    if route.get('protected'):
        return
    host = urlsplit(request_origin).hostname
    token = secrets.token_hex(24)
    proof = secrets.token_hex(24)
    app.domain_challenge = (host, token, proof)
    applied = False
    host_rules_changed = False
    try:
        verify_https(host, token, proof)
        firewall(route['port'], route['gateway'], route['interface'])
        applied = True
        panel_access.sync(app.state_dir, route['port'], route['gateway'], route['interface'])
        host_rules_changed = True
        verify_https(host, token, proof)
        app.save_config(dict(app.config, panel_route=dict(route, protected=True)))
    except Exception:
        try:
            if host_rules_changed:
                panel_access.sync(app.state_dir, route['port'])
        finally:
            if applied:
                firewall()
        raise
    finally:
        app.domain_challenge = None


def repair_target(app):
    route = app.config.get('panel_route', {})
    if not route.get('proxy') or route.get('manual'):
        raise ValueError('当前没有面板自动配置的 NPM 站点。')
    choices = npm_options(detect_environment())
    proxy = next((item for item in choices if item['name'] == route['proxy']), None)
    if not proxy:
        raise ValueError('未找到已配置的 NPM 实例，请检查容器是否运行。')
    return route, proxy, network_endpoint(proxy)


def repair_status(app):
    route, proxy, (_, gateway, interface) = repair_target(app)
    return {
        'proxy': proxy['name'], 'managed': managed_npm(app.manager, proxy),
        'gateway': gateway, 'interface': interface,
        'changed': (gateway, interface, app.config['port']) !=
                   (route['gateway'], route['interface'], route['port']),
    }


def repair(app, data):
    from manager import write_private
    route, proxy, (address, gateway, interface) = repair_target(app)
    port = app.config['port']
    host = normalize(route['origin'])
    adapter = PanelNPM(app.manager, proxy, host, gateway, port, address, data['email'], data['password'])
    state_path = adapter.folder / 'route-state.json'
    previous_state = state_path.read_text() if state_path.exists() else None
    if previous_state is None:
        # Docker can issue a new container ID after a recreation. Recover only
        # a unique prior record that matches the configured upstream.
        matches = []
        for path in (app.state_dir / 'panel-npm').glob('*/' + host + '/route-state.json'):
            saved = json.loads(path.read_text())
            if saved.get('domain') == host and saved.get('forward_host') == route['gateway'] and saved.get('forward_port') == route['port']:
                matches.append(path)
        if len(matches) != 1:
            raise ValueError('无法确认原 NPM 站点的归属，请在 NPM 检查现有站点。')
        saved = json.loads(matches[0].read_text())
    else:
        saved = json.loads(previous_state)
    if (saved.get('domain'), saved.get('forward_host', route['gateway']), saved.get('forward_port', route['port'])) != (host, route['gateway'], route['port']):
        raise ValueError('NPM 站点记录与当前面板配置不一致。')
    token = adapter._npm_token()
    host_id = saved.get('host_id')
    if type(host_id) is not int or host_id <= 0:
        raise ValueError('原 NPM 站点记录无效。')
    path = f'nginx/proxy-hosts/{host_id}'
    current = adapter._npm_request(path, token=token)
    old = {'forward_host': route['gateway'], 'forward_port': route['port']}
    if (not isinstance(current, dict) or current.get('domain_names') != [host] or current.get('forward_scheme') != 'http'
            or any(current.get(key) != value for key, value in old.items())):
        raise ValueError('NPM 中的站点与面板记录不一致，请检查现有站点。')
    new = {'forward_host': gateway, 'forward_port': port}
    token_path = app.state_dir / 'panel-repair-probe.json'
    challenge = [host, secrets.token_hex(24), secrets.token_hex(24)]
    write_private(token_path, json.dumps(challenge))
    protected = route.get('protected')
    firewall_changed = upstream_changed = False
    try:
        if protected:
            firewall(port, gateway, interface)
            firewall_changed = True
            panel_access.sync(app.state_dir, port, gateway, interface)
        if new != old:
            upstream_changed = True
            adapter._npm_request(path, new, token, method='PUT')
        verify_https(*challenge)
        write_private(state_path, json.dumps(dict(saved, **new)))
        app.save_config(dict(app.config, panel_route=dict(route, gateway=gateway, interface=interface, port=port)))
    except Exception as error:
        rollback_failed = False
        if upstream_changed:
            try:
                adapter._npm_request(path, old, token, method='PUT')
            except (ValueError, OSError, subprocess.SubprocessError):
                rollback_failed = True
        if firewall_changed:
            try:
                firewall(route['port'], route['gateway'], route['interface'])
                panel_access.sync(app.state_dir, route['port'], route['gateway'], route['interface'])
            except (ValueError, OSError, subprocess.SubprocessError):
                rollback_failed = True
        if previous_state is None:
            state_path.unlink(missing_ok=True)
        else:
            write_private(state_path, previous_state)
        if rollback_failed:
            raise ValueError('修复失败，部分规则无法恢复。请从 SSH 检查 NPM 和面板防火墙。') from error
        raise
    finally:
        token_path.unlink(missing_ok=True)
    return {'origin': route['origin'], 'proxy': proxy['name'], 'protected': bool(protected)}

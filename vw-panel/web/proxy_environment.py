"""Read only proxy discovery; never inspect container environment secrets."""
import errno
import json
from pathlib import Path
import re
import socket
import subprocess
import time

NETWORK_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}')
CONTAINER_FORMAT = ('{"name":{{json .Name}},"running":{{json .State.Running}},'
                    '"networks":{{json .NetworkSettings.Networks}},"mounts":{{json .Mounts}}}')


def docker(args):
    return subprocess.run(['docker', *args], capture_output=True, text=True, timeout=5)


def existing_network(name):
    if not isinstance(name, str) or not NETWORK_NAME.fullmatch(name) or name in ('bridge', 'host', 'none'):
        raise ValueError('请选择反代容器使用的自定义 Docker 网络，不能使用 bridge、host 或 none。')
    try:
        result = docker(['network', 'inspect', '--format', '{{json .}}', name])
        if result.returncode:
            raise ValueError('未找到 Docker 网络“%s”，请重新检测或填写现有反代的网络。' % name)
        data = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        raise ValueError('无法读取 Docker 网络，请确认 Docker 已启动后重新检测。')
    if not isinstance(data, dict):
        raise ValueError('Docker 网络信息无效，请重新检测。')
    if data.get('Driver') != 'bridge' and not (data.get('Driver') == 'overlay' and data.get('Attachable')):
        raise ValueError('该网络不支持此处的容器接入，请使用自定义 bridge 网络或可接入的 overlay 网络。')
    if data.get('Internal'):
        raise ValueError('该网络禁止外部连接，请选择可访问外网的反代网络。')
    return name


def busy_web_ports():
    busy, checked = set(), True
    for port in (80, 443):
        for family, address in ((socket.AF_INET, '0.0.0.0'), (socket.AF_INET6, '::')):
            try:
                with socket.socket(family, socket.SOCK_STREAM) as probe:
                    if family == socket.AF_INET6:
                        probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                    probe.bind((address, port))
            except OSError as error:
                if error.errno == errno.EADDRINUSE:
                    busy.add(port)
                elif error.errno not in (errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL):
                    checked = False
    return sorted(busy), checked


def proxy_kind(row):
    image = str(row.get('Image', '')).lower().split('@')[0].split(':')[0]
    name = str(row.get('Names', '')).lower()
    if 'nginx-proxy-manager' in image or name in ('npm', 'nginx-proxy-manager'):
        return 'Nginx Proxy Manager'
    if image.rsplit('/', 1)[-1] == 'caddy':
        return 'Caddy'
    if image.rsplit('/', 1)[-1] == 'traefik':
        return 'Traefik'
    if re.search(r':(?:80|443)->', str(row.get('Ports', ''))):
        return 'Nginx / OpenResty' if any(x in image for x in ('nginx', 'openresty')) else '占用 Web 端口的容器'
    return ''


def detect_environment():
    ports, checked = busy_web_ports()
    hosts,host_checked=host_proxies()
    result = {'available': False, 'docker_installed':True, 'host_checked':host_checked, 'complete': True, 'proxies': list(hosts), 'busy_ports': ports,
              'ports_checked': checked, 'message': ''}
    try:
        rows = docker(['ps', '-a', '--format', '{{json .}}'])
        if rows.returncode:
            result['message'] = 'Docker 未运行或无读取权限，无法检测现有反代。'
            result['recommendation']=recommendation(result)
            return result
        result['available'] = True
        deadline = time.monotonic() + 12
        for line in rows.stdout.splitlines():
            row = json.loads(line)
            result['busy_ports'] = sorted(set(result['busy_ports']) | {int(port) for port in re.findall(r':(80|443)->', row.get('Ports', ''))})
        networks = {}
        candidates = [row for line in rows.stdout.splitlines() if line.strip()
                      for row in [json.loads(line)] if proxy_kind(row)]
        if len(candidates) > 32:
            result['complete'] = False
        for row in candidates[:32]:
            if time.monotonic() >= deadline:
                result['complete'] = False
                break
            container = row.get('ID', '')
            if not re.fullmatch(r'[a-f0-9]{12,64}', container):
                result['complete'] = False
                continue
            inspected = docker(['inspect', '--format', CONTAINER_FORMAT, container])
            if inspected.returncode:
                result['complete'] = False
                continue
            info = json.loads(inspected.stdout)
            usable = []
            for name in sorted((info.get('networks') or {})):
                if time.monotonic() >= deadline:
                    result['complete'] = False
                    break
                if name in ('bridge', 'host', 'none'):
                    continue
                if name not in networks:
                    try:
                        existing_network(name)
                        networks[name] = True
                    except ValueError:
                        networks[name] = False
                        result['complete'] = False
                if networks[name]:
                    usable.append(name)
            result['proxies'].append({'id': container, 'name': str(info.get('name', '')).lstrip('/'),
                                      'kind': proxy_kind(row), 'source':'docker', 'running': info.get('running') is True,
                                      'networks': usable})
        if not result['complete']:
            result['message'] = '部分容器信息未能读取，请重新检测；本次不会自动选择网络。'
    except FileNotFoundError:
        result['docker_installed']=False
        result['message'] = '尚未安装 Docker。新服务器可选择安装 Caddy、NPM 或 Nginx，部署前先在运行概览安装 Docker。'
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError):
        result['complete'] = False
        result['message'] = '反代检测未完成，请稍后重新检测或手动填写已确认的网络。'
    result['recommendation']=recommendation(result)
    return result


def ensure_caddy_available(managed_file=None):
    """Reject foreign container names and port conflicts before generating/applying files."""
    try:
        found = docker(['inspect', '--format', CONTAINER_FORMAT, 'caddy'])
        if found.returncode == 0:
            info = json.loads(found.stdout)
            owned = managed_file is not None and any(
                item.get('Destination') == '/etc/caddy/Caddyfile' and
                Path(item.get('Source', '')).resolve() == Path(managed_file).resolve()
                for item in info.get('mounts', []))
            if not owned:
                raise ValueError('已有名为 caddy 的容器。请选择已有容器反代并使用其网络，避免覆盖现有 Caddy。')
            if info.get('running') is True:
                return
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        pass  # Configuration can be saved before Docker is installed.
    ports, _ = busy_web_ports()
    try:
        rows = docker(['ps', '--format', '{{json .}}'])
        if rows.returncode == 0:
            for line in rows.stdout.splitlines():
                ports = sorted(set(ports) | {int(port) for port in re.findall(r':(80|443)->', json.loads(line).get('Ports', ''))})
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    if ports:
        raise ValueError('端口 %s 已被占用，无法启动新的 Caddy。请选择已有反代方式。' % ' / '.join(map(str, ports)))

HOST_SERVICES = {'nginx':'Nginx','caddy':'Caddy','traefik':'Traefik','haproxy':'HAProxy','apache2':'Apache','httpd':'Apache'}


def host_proxies():
    """Read listener process names; never guess a host proxy from a busy port alone."""
    try:
        result=subprocess.run(['ss','-H','-lntp'],capture_output=True,text=True,timeout=5)
        if result.returncode:return [],False
        found={}
        for line in result.stdout.splitlines():
            parts=line.split()
            if len(parts)<5:continue
            match=re.search(r':(\d+)$',parts[3])
            if not match:continue
            for process in re.findall(r'\("([^\"]+)",pid=\d+',line):
                if process not in HOST_SERVICES:continue
                item=found.setdefault(process,{'id':'host:'+process,'name':process,'kind':HOST_SERVICES[process],
                    'source':'host','running':True,'networks':[],'ports':[]})
                port=int(match[1])
                if port not in item['ports']:item['ports'].append(port)
        return list(found.values()),True
    except (OSError,subprocess.SubprocessError):return [],False


def recommendation(environment):
    running=[p for p in environment.get('proxies',[]) if p.get('running')]
    known=[p for p in running if p.get('kind')!='占用 Web 端口的容器']
    if len(known)==1 and len(running)==1:
        proxy=known[0]
        if proxy.get('source')=='host':return {'state':'existing','mode':'host_ext','target':proxy['id'],'name':proxy['name'],'kind':proxy['kind']}
        if proxy.get('networks') and environment.get('complete'):
            return {'state':'existing','mode':'docker_ext','target':proxy['id'],'name':proxy['name'],'kind':proxy['kind']}
    if len(running)>1:return {'state':'choose','reason':'检测到多个入口服务，请选择要复用的反代。'}
    if known:return {'state':'choose','reason':'检测到反代，但没有可直接接入的自定义网络。请先配置其网络，再重新检测；也可手动选择接入方式。'}
    if environment.get('proxies') and not running:return {'state':'choose','reason':'发现尚未运行的反代。请先启动后重新检测，或手动选择新装服务。'}
    if running:return {'state':'unknown','reason':'发现占用 Web 入口的容器，但未确认其反代类型。请重新检测或手动选择，面板不会自动覆盖入口。'}
    if environment.get('busy_ports'):return {'state':'unknown','reason':'80 / 443 已有服务占用，但暂未识别出反代类型。请重新检测或手动选择，面板不会自动覆盖入口。'}
    if environment.get('ports_checked') and (environment.get('complete') and environment.get('available') or environment.get('docker_installed') is False):
        return {'state':'install','mode':'caddy','reason':'未发现正在使用的反代入口，80 / 443 空闲，可直接安装反代。'}
    return {'state':'unknown','reason':'暂时无法完整确认服务器环境。请重新检测，或手动指定接入方式。'}


def log_sources(environment,memory,configured):
    all_sources=list(environment.get('proxies',[]))
    target=memory.get('PANEL_PROXY_TARGET','')
    kind=memory.get('PANEL_PROXY_KIND')
    selected=[]
    if kind in ('npm','nginx'):
        selected=[p for p in all_sources if p.get('name')=='vw-panel-'+kind]
    elif target:selected=[p for p in all_sources if p['id']==target]
    if selected:return selected
    if configured:
        mode=memory.get('PROXY_MODE')
        if mode=='caddy':selected=[p for p in all_sources if p.get('kind')=='Caddy']
        elif mode=='docker_ext':selected=[p for p in all_sources if p.get('source','docker')=='docker' and memory.get('DOCKER_NET') in p.get('networks',[])]
        elif mode=='host_ext':selected=[p for p in all_sources if p.get('source')=='host']
    if selected:return selected
    # Missing or changed configuration does not justify falling back to a container named caddy.
    return all_sources

"""Manage only the panel's own ingress exceptions in active host firewalls."""
import json
import os
import re
import shutil
import subprocess

from manager import write_private


def run(*args):
    return subprocess.run(args, text=True, capture_output=True, timeout=8,
                          env=dict(os.environ, LC_ALL='C'))


def checked(*args):
    result = run(*args)
    if result.returncode:
        raise ValueError('无法更新本机防火墙中的面板端口规则。')
    return result.stdout


def rules(port, gateway=None, interface=None):
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('面板端口无效。')
    result = []
    if shutil.which('ufw') and checked('ufw', 'status').startswith('Status: active'):
        args = ['in']
        if gateway:
            args += ['on', interface]
        args += ['proto', 'tcp', 'from', 'any', 'to', gateway or 'any', 'port', str(port)]
        result.append(['ufw', *args])
    if shutil.which('firewall-cmd') and run('firewall-cmd', '--state').returncode == 0:
        active = checked('firewall-cmd', '--get-active-zones')
        zones = {line.strip() for line in active.splitlines() if line and not line[0].isspace()}
        zones.add(checked('firewall-cmd', '--get-default-zone').strip())
        for zone in sorted(zones):
            if not re.fullmatch(r'[\w-]+', zone):
                raise ValueError('无法识别防火墙区域。')
            destination = f'family="ipv4" destination address="{gateway}" ' if gateway else ''
            rule = f'rule priority="-24139" {destination}port port="{port}" protocol="tcp" accept'
            for permanent in (False, True):
                result.append(['firewall-cmd', *(['--permanent'] if permanent else []), '--zone='+zone, rule])
    return result


def change(rule, adding):
    if rule[0] == 'ufw':
        # Numbered output includes the exact comment we own; delete from the bottom
        # because rule indexes shift after each removal.
        status = checked('ufw', 'status', 'numbered')
        marker = 'VW PANEL ' + rule[-1] + (' proxy' if 'on' in rule else ' public')
        indexes = re.findall(r'^\[\s*(\d+)\].*#\s*'+re.escape(marker)+r'\s*$', status, re.M)
        if adding and not indexes:
            # Keep pre-existing broad allows, including their comments. The
            # dedicated nftables restriction still blocks external traffic.
            if 'on' not in rule and re.search(r'^\[\s*\d+\]\s+'+re.escape(rule[-1])+r'/tcp(?: \(v6\))?\s+ALLOW IN\s+Anywhere(?: \(v6\))?(?:\s+#.*)?$', status, re.M):
                return
            checked('ufw', 'insert', '1', 'allow', *rule[1:], 'comment', marker)
        elif not adding:
            for index in reversed(indexes):
                checked('ufw', '--force', 'delete', index)
    else:
        prefix, rich_rule = rule[:-1], rule[-1]
        exists = run(*prefix, '--query-rich-rule='+rich_rule).returncode == 0
        if exists != adding:
            checked(*prefix, ('--add-rich-rule=' if adding else '--remove-rich-rule=')+rich_rule)


def sync(state_dir, port=None, gateway=None, interface=None):
    path = state_dir / 'panel-firewall.json'
    previous = json.loads(path.read_text()) if path.exists() else []
    desired = rules(port, gateway, interface) if port is not None else []
    added, removed = [], []
    try:
        for rule in desired:
            if rule not in previous:
                change(rule, True)
                added.append(rule)
        for rule in previous:
            if rule not in desired:
                change(rule, False)
                removed.append(rule)
        if desired or path.exists():
            write_private(path, json.dumps(desired))
    except (OSError, ValueError, subprocess.SubprocessError):
        for rule, adding in [(r, True) for r in removed] + [(r, False) for r in reversed(added)]:
            try:
                change(rule, adding)
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        raise
    return bool(desired)

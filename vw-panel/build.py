#!/usr/bin/env python3
"""Reproducible web-only and full installers."""
import base64
import gzip
import hashlib
import io
import json
import re
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parent
# Maintain vw_web.sh directly; packaging must never overwrite source files.
if hashlib.sha256((ROOT / 'vw_original.sh').read_bytes()).hexdigest() != '756b783a388b087ff48af8db93273543a2ea2ea7009bdcfd50201e671d044aff':
    raise SystemExit('Original script snapshot changed; refusing to package.')
paths = ['panel-cli.sh', 'vw_web.sh', 'vw_original.sh', 'web/server.py', 'web/panel_domain.py', 'web/panel_access.py', 'web/session_store.py', 'web/manager.py', 'web/compose_yaml.py', 'web/proxy_environment.py', 'web/managed_proxy.py', 'web/existing_npm.py', 'web/updater.py', 'web/operations.py','web/remote_config.py','web/browser_oauth.py','web/backup_targets.py','web/backup_result.py', 'web/storage_presets.py', 'web/environment.py', 'web/env_catalog.json', 'web/tty_worker.py', 'web/static/index.html', 'web/static/app.js', 'web/static/style.css']
header_template = (ROOT / 'installer-header.sh').read_text()
script_files = {'vw_web.sh', 'vw_original.sh', 'web/tty_worker.py'}


def build(edition, installer_name, manifest_name):
    files = {}
    for name in paths:
        if edition == 'web' and name in script_files:
            continue
        value = (ROOT / name).read_bytes()
        if edition == 'web' and name == 'web/static/index.html':
            value = re.sub(rb'<!-- FULL_ONLY_START -->.*?<!-- FULL_ONLY_END -->', b'', value, flags=re.S)
        files[name] = value

    source = hashlib.sha256(edition.encode() + b'\0')
    for name, value in files.items():
        source.update(name.encode() + b'\0' + value + b'\0')
    for name in ('installer-header.sh', 'build.py'):
        source.update(name.encode() + b'\0' + (ROOT / name).read_bytes() + b'\0')

    version = {'version': '0.7.43', 'build_id': source.hexdigest(), 'edition': edition}
    files['version.json'] = (json.dumps(version, sort_keys=True) + '\n').encode()
    if edition == 'full':
        (ROOT / 'version.json').write_bytes(files['version.json'])

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as archive:
        for name, value in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(value), 0o644, 0
            archive.addfile(info, io.BytesIO(value))
    payload = gzip.compress(buffer.getvalue(), mtime=0)
    encoded = base64.b64encode(payload).decode()
    header = (header_template.replace('@PAYLOAD_SHA256@', hashlib.sha256(payload).hexdigest())
              .replace('@EDITION@', edition).replace('@INSTALLER_NAME@', installer_name))
    output = ROOT / installer_name
    output.write_text(header + '\n__VW_PANEL_PAYLOAD__\n'
                      + '\n'.join(encoded[i:i+76] for i in range(0, len(encoded), 76)) + '\n')
    output.chmod(0o755)
    release = dict(version, installer_sha256=hashlib.sha256(output.read_bytes()).hexdigest())
    (ROOT / manifest_name).write_text(json.dumps(release, sort_keys=True) + '\n')
    print('Built', output.name, output.stat().st_size, 'bytes')


if __name__ == '__main__':
    build('full', 'install-vw-web.sh', 'release.json')
    build('web', 'install-vw-web-only.sh', 'release-web.json')

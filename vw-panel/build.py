#!/usr/bin/env python3
"""Reproducible standalone installer: bundled panel and pinned upstream script."""
import base64
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parent
# Maintain vw_web.sh directly; packaging must never overwrite source files.
if hashlib.sha256((ROOT / 'vw_original.sh').read_bytes()).hexdigest() != '756b783a388b087ff48af8db93273543a2ea2ea7009bdcfd50201e671d044aff':
    raise SystemExit('Original script snapshot changed; refusing to package.')
paths = ['panel-cli.sh', 'vw_web.sh', 'vw_original.sh', 'web/server.py', 'web/manager.py', 'web/updater.py', 'web/operations.py', 'web/tty_worker.py', 'web/static/index.html', 'web/static/app.js', 'web/static/style.css']
source = hashlib.sha256()
for name in paths + ['installer-header.sh', 'build.py']:
    source.update(name.encode() + b'\0' + (ROOT / name).read_bytes() + b'\0')
version = {'version': '0.5.4', 'build_id': source.hexdigest()}
(ROOT / 'version.json').write_text(json.dumps(version, sort_keys=True) + '\n')
paths.append('version.json')
buffer = io.BytesIO()
with tarfile.open(fileobj=buffer, mode='w') as archive:
    for path in paths:
        value = (ROOT / path).read_bytes()
        info = tarfile.TarInfo(path)
        info.size, info.mode, info.mtime = len(value), 0o644, 0
        archive.addfile(info, io.BytesIO(value))
payload = gzip.compress(buffer.getvalue(), mtime=0)
encoded = base64.b64encode(payload).decode()
header = (ROOT / 'installer-header.sh').read_text().replace('@PAYLOAD_SHA256@', hashlib.sha256(payload).hexdigest())
output = ROOT / 'install-vw-web.sh'
output.write_text(header + '\n__VW_PANEL_PAYLOAD__\n' + '\n'.join(encoded[i:i+76] for i in range(0, len(encoded), 76)) + '\n')
output.chmod(0o755)
(ROOT / 'release.json').write_text(json.dumps(dict(version, installer_sha256=hashlib.sha256(output.read_bytes()).hexdigest()), sort_keys=True) + '\n')
print('Built', output.name, output.stat().st_size, 'bytes')

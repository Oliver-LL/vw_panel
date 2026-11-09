import json
from pathlib import Path
import shutil
import subprocess
import unittest


def compose_command():
    plugin=Path('/Applications/Docker.app/Contents/Resources/cli-plugins/docker-compose')
    if plugin.exists():return [str(plugin)]
    if shutil.which('docker'):return ['docker','compose']
    raise unittest.SkipTest('Docker Compose required to validate generated YAML')


def load_compose(path):
    result=subprocess.run([*compose_command(),'-f',str(path),'config','--no-interpolate','--format','json'],capture_output=True,text=True,timeout=20)
    if result.returncode:raise AssertionError(result.stderr)
    return json.loads(result.stdout)

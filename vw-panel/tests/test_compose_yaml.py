import json
from pathlib import Path
import tempfile
import unittest
from compose_yaml import compose_yaml
from compose_test_utils import load_compose

class ComposeYAMLTests(unittest.TestCase):
    def test_special_strings_remain_strings_and_cannot_inject_yaml(self):
        values=['yes','on','null','123','','a: b # comment','line one\nline two','quote"and\\slash','P$a$$word','中文','- item']
        labels={str(i):value for i,value in enumerate(values)}
        document={'services':{'vaultwarden':{'image':'vaultwarden/server:latest','labels':labels,'networks':['npm_default']}},'networks':{'npm_default':{'external':True}}}
        raw=compose_yaml(document)
        self.assertTrue(raw.startswith('services:\n'));self.assertIn('    image: ',raw);self.assertNotIn('"services": {',raw)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'docker-compose.yml';path.write_text(raw)
            parsed=load_compose(path)
        self.assertEqual(parsed['services']['vaultwarden']['labels'],labels)
        self.assertTrue(parsed['networks']['npm_default']['external'])

    def test_empty_collections_and_compose_commands(self):
        document={'services':{'worker':{'image':'alpine','command':['sh','-c','printf "%s" "$${SECRET}"'],'environment':{'BOOL':'false'},'labels':{}}}}
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'docker-compose.yml';path.write_text(compose_yaml(document));parsed=load_compose(path)
        self.assertEqual(parsed['services']['worker']['command'],document['services']['worker']['command'])
        self.assertEqual(parsed['services']['worker']['environment'],{'BOOL':'false'})

    def test_unsupported_objects_fail(self):
        with self.assertRaises(TypeError):compose_yaml({'services':object()})

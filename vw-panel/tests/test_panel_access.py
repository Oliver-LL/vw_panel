from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import call, patch

import panel_access


class PanelAccessTests(unittest.TestCase):
    def test_transition_replaces_public_rule_with_bridge_rule(self):
        with TemporaryDirectory() as temp, patch('panel_access.rules') as rules, patch('panel_access.change') as change:
            state = Path(temp)
            public = ['ufw', 'public', '49152']
            bridge = ['ufw', 'bridge', '49152']
            rules.side_effect = [[public], [bridge]]
            panel_access.sync(state, 49152)
            panel_access.sync(state, 49152, '172.20.0.1', 'br-test')
            panel_access.sync(state)
            self.assertEqual(change.call_args_list, [
                call(public, True), call(bridge, True), call(public, False), call(bridge, False),
            ])

    def test_ufw_keeps_unrelated_allow_and_removes_only_panel_rules(self):
        rule = ['ufw', 'in', 'proto', 'tcp', 'from', 'any', 'to', 'any', 'port', '49152']
        existing = '[ 1] 49152/tcp ALLOW IN Anywhere # operator\n'
        with patch('panel_access.checked', return_value=existing) as command:
            panel_access.change(rule, True)
            command.assert_called_once_with('ufw', 'status', 'numbered')
        with patch('panel_access.checked', return_value=existing+'[ 2] 49152/tcp (v6) ALLOW IN Anywhere (v6) # VW PANEL 49152 public\n') as command:
            panel_access.change(rule, False)
            self.assertEqual(command.call_args_list, [
                call('ufw', 'status', 'numbered'), call('ufw', '--force', 'delete', '2'),
            ])

import unittest
from agent_observatory.cli import build_parser
class RetiredPanelTests(unittest.TestCase):
 def test_panel_commands_absent(self):
  help_text=build_parser().format_help()
  for command in ('silver-build','silver-evaluate','silver-collapse','silver-two-stage','silver-two-stage-run'):
   self.assertNotIn(command,help_text)

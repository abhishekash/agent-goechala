import unittest
from unittest.mock import Mock

from rich.text import Text

from observatory.ui_render import update_status_text


class StatusRenderingBudgetTests(unittest.TestCase):
    def test_unchanged_status_does_not_request_a_repaint(self):
        class Widget:
            update = Mock()

        widget = Widget()
        self.assertTrue(update_status_text(widget, Text('Connected', style='green')))
        self.assertFalse(update_status_text(widget, Text('Connected', style='green')))
        widget.update.assert_called_once()
        self.assertTrue(update_status_text(widget, Text('Connected', style='blue')))
        self.assertTrue(update_status_text(widget, Text('Disconnected', style='blue')))
        self.assertEqual(widget.update.call_count, 3)

"""The tower must never displace the product name outside the header."""
import unittest

from observatory.branding import masthead
from observatory.terminal_ui import ObservatoryApp
from tests.test_terminal_search import ManualFeed


class BrandingTest(unittest.IsolatedAsyncioTestCase):
    async def test_name_is_visible_at_wide_and_compact_sizes(self):
        app = ObservatoryApp(ManualFeed())
        async with app.run_test(size=(160, 48)) as pilot:
            for size in ((160, 48), (120, 40), (80, 24)):
                await pilot.resize_terminal(*size)
                await pilot.pause()
                brand = app.query_one('#brand')
                rendered = ' '.join(brand.render_line(row).text for row in range(brand.size.height))
                self.assertIn('♜', rendered)
                self.assertIn('Agenturm', rendered)
                self.assertEqual(brand.content_size.height, 1)

    def test_masthead_is_one_line_even_in_demo_mode(self):
        for compact in (False, True):
            for demo in (False, True):
                label = masthead(compact=compact, demo=demo).plain
                self.assertIn('Agenturm', label)
                self.assertNotIn('\n', label)

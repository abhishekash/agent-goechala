"""Header identity, summary, and controls stay visible at supported sizes."""
import unittest

from rich.cells import cell_len

from observatory.branding import NAME, TAGLINE, masthead
from observatory.terminal_ui import ObservatoryApp
from observatory.themes import PALETTES
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
                self.assertIn('Agenturm', rendered)
                self.assertNotIn('♜', rendered)
                self.assertEqual(brand.content_size.height, 1)
                self.assertTrue(app.query_one('#masthead').region.contains_region(brand.region))
                self.assertLessEqual(app.query_one('#brand-lockup').region.right,
                                     app.query_one('#connection').region.x)
                if size[0] >= 100 and size[1] >= 36:
                    self.assertIn(TAGLINE, rendered)

    async def test_header_stays_two_rows_tall(self):
        app = ObservatoryApp(ManualFeed())
        async with app.run_test(size=(160, 48)) as pilot:
            for size in ((160, 48), (120, 40), (80, 24)):
                await pilot.resize_terminal(*size)
                await pilot.pause()
                masthead = app.query_one('#masthead')
                self.assertLessEqual(masthead.size.height, 2 if size[1] >= 36 else 1)
                self.assertEqual(app.query_one('#brand').size.height, 1)

    async def test_complete_shortcut_legend_stays_at_the_top(self):
        app = ObservatoryApp(ManualFeed())
        async with app.run_test(size=(160, 48)) as pilot:
            for size in ((160, 48), (120, 40), (100, 40), (80, 24)):
                await pilot.resize_terminal(*size)
                await pilot.pause()
                hints = app.query_one('#shortcuts')
                self.assertTrue(hints.display, f'{size}: hints should be visible')
                self.assertLess(hints.region.y, app.query_one('#pages').region.y)
                rendered = '\n'.join(hints.render_line(row).text for row in range(hints.size.height))
                for label in ('views', 'search', 'expand', 'trace', 'copy', 'pause',
                              'diagnostics', 'export', 'quit', 'session activity',
                              'reset', 'reviews', 'newer', 'older', 'latest', 'back', 'palette'):
                    self.assertIn(label, rendered, f'{size}: missing {label}')

    async def test_bottom_footer_is_removed(self):
        app = ObservatoryApp(ManualFeed())
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause()
            self.assertEqual(len(app.query('Footer')), 0)

    async def test_masthead_lockup_is_centered(self):
        app = ObservatoryApp(ManualFeed())
        async with app.run_test(size=(200, 50)) as pilot:
            for width, height in ((200, 50), (120, 40)):
                await pilot.resize_terminal(width, height)
                await pilot.pause()
                masthead = app.query_one('#masthead')
                lockup = app.query_one('#brand-lockup')
                self.assertAlmostEqual(
                    lockup.region.x + lockup.region.width / 2,
                    masthead.region.x + masthead.region.width / 2, delta=2)
                connection = app.query_one('#connection')
                self.assertGreaterEqual(connection.region.right, masthead.region.right - 3)

    async def test_summary_and_controls_fit_without_clipping(self):
        feed = ManualFeed()
        feed.api.endpoint = 'http://127.0.0.1:4318/' + 'long-endpoint/' * 12
        feed.snapshot.totals.update(input_tokens=25_600_000, output_tokens=72_500)
        feed.snapshot.mcp_summary.update(calls=145, errors=28)
        app = ObservatoryApp(feed)
        async with app.run_test(size=(160, 48)) as pilot:
            for size in ((160, 48), (120, 40), (100, 40), (80, 40), (80, 24), (160, 48)):
                await pilot.resize_terminal(*size)
                await pilot.pause()
                summary = app.query_one('#summary')
                stats = list(app.query('.stat'))
                for stat in stats:
                    self.assertTrue(summary.region.contains_region(stat.region))
                    text = stat.render()
                    lines = text.plain.splitlines()[:stat.content_size.height]
                    for line in lines:
                        self.assertLessEqual(cell_len(line), stat.content_size.width,
                                             f'{size}: {stat.id}: {line}')
                self.assertEqual(len({stat.region.y for stat in stats}), 2 if size[0] < 100 else 1)
                for name in ('scope', 'reviews', 'pause', 'connection'):
                    widget = app.query_one('#' + name)
                    self.assertTrue(app.screen.region.contains_region(widget.region), f'{size}: {name}')
                reviews = app.query_one('#reviews')
                self.assertIn('Reviews', reviews.render_line(0).text)
                scope_label = app.query_one('#scope SelectCurrent #label')
                self.assertEqual(scope_label.size.height, 1)
                self.assertIn('All agent sessions', scope_label.render_line(0).text)
                self.assertFalse(app.query_one('#history-back').display)
                self.assertIn('127.0.0.1:4318', app.query_one('#connection').tooltip)
                table = app.query_one('#agent-table')
                visible = table.region.intersection(app.query_one('#results-agents').content_region)
                self.assertGreaterEqual(visible.height, 3, f'{size}: session rows must remain visible')

    def test_masthead_is_one_line_even_in_demo_mode(self):
        for compact in (False, True):
            for demo in (False, True):
                label = masthead(compact=compact, demo=demo).plain
                self.assertIn('Agenturm', label)
                self.assertNotIn('\n', label)

    async def test_wordmark_colors_follow_the_theme(self):
        app = ObservatoryApp(ManualFeed())
        async with app.run_test(size=(160, 48)) as pilot:
            for size in ((160, 48), (80, 24)):
                await pilot.resize_terminal(*size)
                for name, colors in PALETTES.items():
                    app.theme = name
                    await pilot.pause()
                    brand = app.query_one('#brand')
                    name_segment = next(segment for segment in brand.render_line(0)
                                        if 'Agenturm' in segment.text)
                    self.assertEqual(name_segment.style.color.get_truecolor().hex, colors['text'])
                    if size[0] >= 100 and size[1] >= 36:
                        tagline = next(segment for segment in brand.render_line(0)
                                       if TAGLINE in segment.text)
                        self.assertEqual(tagline.style.color.get_truecolor().hex, colors['muted'])

    def test_palette_has_sufficient_contrast_in_every_theme(self):
        def luminance(color):
            channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
            linear = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4
                      for value in channels]
            return sum(value * weight for value, weight in zip(linear, (.2126, .7152, .0722)))

        for name, colors in PALETTES.items():
            for role in ('text', 'muted', 'secondary', 'primary'):
                with self.subTest(theme=name, role=role):
                    values = sorted((luminance(colors[role]), luminance(colors['canvas'])))
                    self.assertGreaterEqual((values[1] + .05) / (values[0] + .05), 4.5)

    def test_wordmark_is_bounded_standard_terminal_text(self):
        self.assertEqual(masthead().plain.splitlines(), [f'{NAME}  │  {TAGLINE}'])
        self.assertEqual(masthead(compact=True).plain, NAME)

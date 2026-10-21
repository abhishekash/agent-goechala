"""The palette exposes only supported themes, and each changes the actual UI."""
import unittest
from types import SimpleNamespace

from textual.command import CommandPalette
from textual.widgets import Input

from observatory.terminal_ui import ObservatoryApp, evidence_syntax
from observatory.themes import PALETTES, themed_style
from tests.test_terminal_search import ManualFeed


class ThemePickerTest(unittest.IsolatedAsyncioTestCase):
    async def test_only_four_themes_and_each_is_selectable(self):
        app = ObservatoryApp(ManualFeed())
        async with app.run_test(size=(120, 40)) as pilot:
            self.assertEqual(set(app.available_themes), {'Current', 'Bright', 'Dark', 'Solarized'})
            self.assertEqual(app.theme, 'Current')
            backgrounds = set()
            for name in ('Bright', 'Dark', 'Solarized', 'Current'):
                app.search_themes()
                await pilot.pause()
                self.assertIsInstance(app.screen, CommandPalette)
                app.screen.query_one(Input).value = name
                await pilot.pause(.2)
                await pilot.press('down', 'enter')
                await pilot.pause()
                self.assertNotIsInstance(app.screen, CommandPalette)
                self.assertEqual(app.theme, name)
                colors = PALETTES[name]
                self.assertEqual(app.screen.styles.background.hex.lower(), colors['canvas'])
                self.assertEqual(app.query_one('#agent-table').styles.background.hex.lower(), colors['surface'])
                # Cached evidence and Rich table colors must change, not just the app shell.
                table = app.query_one('#agent-table')
                session = table.get_row_at(0)[1]
                self.assertEqual(str(session.style), colors['secondary'])
                log = app.query_one('#agent-detail')
                segments = [segment for line in log.lines for segment in line]
                self.assertTrue(any(segment.style and segment.style.color and
                                    segment.style.color.get_truecolor().hex == colors['text']
                                    for segment in segments))
                backgrounds.add(app.screen.styles.background.hex)
            self.assertEqual(len(backgrounds), 4, 'Theme selection must change the rendered background')

    def test_evidence_syntax_background_matches_each_palette(self):
        for name, colors in PALETTES.items():
            syntax = evidence_syntax('{"status": "ok"}', 'json', name)
            self.assertEqual(syntax.background_color, colors['surface'])

    def test_theme_translation_does_not_remap_replacement_colors(self):
        widget = SimpleNamespace(app=SimpleNamespace(theme='Bright'))
        self.assertEqual(themed_style(widget, '#121018 on #c5adff'), '#ffffff on #594095')

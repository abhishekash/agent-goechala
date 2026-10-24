"""Optional media-production checks: run with CairoSVG and Pillow installed."""

from io import BytesIO
from copy import deepcopy
import unittest

try:
    import cairosvg
    from PIL import Image, ImageChops
    from scripts import compose_investigation_video as video
except ImportError:
    video = None


@unittest.skipIf(video is None, 'requires media extras: cairosvg, pillow, imageio-ffmpeg')
class BrandMediaTests(unittest.TestCase):
    def test_brand_generator_preserves_authoritative_icon(self):
        from scripts.build_brand_assets import mark, svg

        expected = svg(512, 512, mark(), 'Agenturm tower mark')
        self.assertEqual((video.ROOT / 'media/brand/icon.svg').read_text(), expected)

    def test_demo_feed_acknowledges_prompt_filter(self):
        from scripts.render_terminal_demo import FakeFeed

        feed = FakeFeed()
        feed.snapshot = deepcopy(feed.snapshot)
        feed.snapshot.activity = [
            {'domain': 'message', 'action': 'user', 'summary': 'First prompt'},
            {'domain': 'message', 'action': 'user', 'summary': 'Second prompt'},
            {'domain': 'model', 'action': 'call', 'summary': 'Model call'},
        ]
        for prompts_only in (True, False):
            feed.activity_prompts = prompts_only
            feed.request_refresh()
            self.assertEqual(feed.snapshot.activity_prompts, prompts_only)
            self.assertEqual(len(feed.snapshot.activity_results), 2 if prompts_only else 3)

    def test_readme_canvas_is_even_without_resizing_content(self):
        source = Image.new('RGB', (1920, 1667))
        width, height = video.canvas_size(source, 'readme')
        self.assertEqual(width, 1600)
        self.assertEqual(height % 2, 0)
        self.assertLessEqual(abs(height - (1600 * source.height / source.width + video.README_CAPTION)), 1.5)

    def test_video_icon_matches_authoritative_svg_at_display_sizes(self):
        for size in (68, 240, 320):
            with self.subTest(size=size):
                expected = Image.open(BytesIO(cairosvg.svg2png(
                    url=str(video.ROOT / 'media/brand/icon.svg'),
                    output_width=size, output_height=size,
                ))).convert('RGBA')
                actual = video.brand_icon(size)
                self.assertEqual(actual.size, (size, size))
                self.assertEqual(actual.tobytes(), expected.tobytes())

    def test_readme_outro_preserves_square_icon_and_cutouts(self):
        frame = video.outro(None, layout='readme', size=(1600, 1580))
        icon = video.brand_icon(240)
        expected = Image.new('RGB', icon.size, video.BG)
        expected.paste(icon, (0, 0), icon)
        actual = frame.crop((680, 220, 920, 460))
        self.assertIsNone(ImageChops.difference(actual, expected).getbbox())


if __name__ == '__main__':
    unittest.main()

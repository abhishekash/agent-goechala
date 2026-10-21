"""Render ten terminal header options for review.

Every option is drawn by the real app, so the previews show the real fonts,
theme, and panel chrome. Requires cairosvg and Pillow. No receiver or network
is used.
"""

import asyncio
import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cairosvg
from PIL import Image, ImageDraw, ImageFont
from rich.text import Text

from observatory.branding import AMBER, FROST, LAVENDER, MUTED, NAME, TAGLINE
from observatory.terminal_ui import ObservatoryApp
from scripts.render_terminal_demo import FakeFeed

OUT = ROOT / "media" / "header-options"
CANVAS = "#121018"
LABEL_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
BODY_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
SIZE = (150, 46)


def wordmark():
    return Text(NAME, style=f"bold {FROST}")


def lockup(divider="  │  ", tagline=TAGLINE, tagline_style=MUTED):
    text = Text(NAME, style=f"bold {FROST}")
    if tagline:
        text.append(divider, style=MUTED)
        text.append(tagline, style=tagline_style)
    return text


def stacked(tagline=TAGLINE):
    text = Text(NAME, style=f"bold {FROST}")
    text.append("\n" + tagline, style=MUTED)
    return text


OPTIONS = [
    {
        "key": "01-wordmark-tagline",
        "title": "Wordmark and tagline",
        "note": "One line: name, divider, tagline, centered",
        "rows": 1, "height": 2, "columns": ("1fr", "auto", "1fr"), "status_row": 0,
        "brand": lockup(),
    },
    {
        "key": "02-stacked",
        "title": "Stacked",
        "note": "Name over the tagline, two rows, centered",
        "rows": 2, "height": 3, "columns": ("1fr", "auto", "1fr"), "status_row": 0,
        "brand": stacked(),
    },
    {
        "key": "03-caps",
        "title": "Caps lock",
        "note": "Uppercase name and tagline on one line",
        "rows": 1, "height": 2, "columns": ("1fr", "auto", "1fr"), "status_row": 0,
        "brand": lockup(tagline=TAGLINE.upper()),
    },
    {
        "key": "04-left-aligned",
        "title": "Left aligned",
        "note": "Same lockup anchored to the left edge",
        "rows": 1, "height": 2, "columns": ("0", "auto", "1fr"), "status_row": 0,
        "brand": lockup(),
    },
    {
        "key": "05-spaced-caps",
        "title": "Spaced caps",
        "note": "Letter-spaced name with the tagline beneath",
        "rows": 2, "height": 3, "columns": ("1fr", "auto", "1fr"), "status_row": 0,
        "brand": stacked(tagline="A  C L E A R  V I E W  O F  E V E R Y  A G E N T"),
    },
    {
        "key": "06-wordmark-only",
        "title": "Wordmark only",
        "note": "The name alone, nothing else",
        "rows": 1, "height": 2, "columns": ("1fr", "auto", "1fr"), "status_row": 0,
        "brand": wordmark(),
    },
    {
        "key": "07-italic-tagline",
        "title": "Italic tagline",
        "note": "Name, divider, italic tagline",
        "rows": 1, "height": 2, "columns": ("1fr", "auto", "1fr"), "status_row": 0,
        "brand": lockup(tagline_style=f"italic {MUTED}"),
    },
    {
        "key": "08-amber-name",
        "title": "Amber name",
        "note": "Name in the accent color, tagline muted",
        "rows": 1, "height": 2, "columns": ("1fr", "auto", "1fr"), "status_row": 0,
        "brand": lockup(divider="  ·  "),
    },
    {
        "key": "09-prompt",
        "title": "Prompt",
        "note": "Shell prompt, lowercase name, tagline",
        "rows": 1, "height": 2, "columns": ("1fr", "auto", "1fr"), "status_row": 0,
        "brand": Text("❯ ", style=f"bold {AMBER}")
        + Text(NAME.lower(), style=f"bold {FROST}")
        + Text("  ·  " + TAGLINE.lower(), style=MUTED),
    },
    {
        "key": "10-lavender-underline",
        "title": "Lavender underline",
        "note": "Name underlined in lavender, tagline beneath",
        "rows": 2, "height": 3, "columns": ("1fr", "auto", "1fr"), "status_row": 0,
        "brand": Text(NAME, style=f"bold underline {LAVENDER}")
        + Text("\n" + TAGLINE, style=MUTED),
    },
]


def apply_option(app, option):
    """Push one option's geometry and copy into the live header."""
    masthead = app.query_one("#masthead")
    masthead.styles.height = option["height"]
    masthead.styles.grid_size_rows = 1
    masthead.styles.grid_rows = (str(option["rows"]),)
    masthead.styles.grid_columns = option["columns"]
    masthead.styles.padding = (0, 2)

    lockup_widget = app.query_one("#brand-lockup")
    lockup_widget.styles.height = option["rows"]

    brand = app.query_one("#brand")
    brand.styles.height = option["rows"]
    brand.update(option["brand"])

    status = app.query_one("#connection")
    status.styles.margin = (option["status_row"], 0)


def raster(svg):
    svg = svg.replace("Fira Code", "DejaVu Sans Mono").replace("FiraCode", "DejaVu Sans Mono")
    return Image.open(io.BytesIO(cairosvg.svg2png(bytestring=svg.encode(), output_width=1500))).convert("RGB")


def header_rows(svg):
    view = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg)
    width, height = float(view.group(1)), float(view.group(2))
    return height / SIZE[1], width / SIZE[0]


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    app = ObservatoryApp(FakeFeed())
    crops = []
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(.2)
        # A late resize event would restore the app's own header copy.
        app.update_layout = lambda *args, **kwargs: None
        for option in OPTIONS:
            apply_option(app, option)
            await pilot.pause(.4)
            svg = app.export_screenshot()
            image = raster(svg)
            image.save(OUT / f"{option['key']}.png")
            row_height, cell_width = header_rows(svg)
            crop = image.crop((0, 0, image.width, int(round((option["height"] + 1) * row_height))))
            crops.append((option, crop))
            print(f"{option['key']}: {option['note']}")

    label_height = 46
    sheet = Image.new("RGB", (max(crop.width for _, crop in crops),
                              sum(crop.height + label_height for _, crop in crops)), "#0b0910")
    draw = ImageDraw.Draw(sheet)
    title = ImageFont.truetype(LABEL_FONT, 20)
    note = ImageFont.truetype(BODY_FONT, 15)
    y = 0
    for option, crop in crops:
        draw.text((14, y + 8), f"{option['key']}  {option['title']}", font=title, fill="#ffbd69")
        draw.text((14, y + 30), option["note"], font=note, fill="#b9afc5")
        sheet.paste(crop, (0, y + label_height))
        y += crop.height + label_height
    sheet.save(OUT / "00-contact-sheet.png")
    print(f"Wrote {len(OPTIONS)} options and a contact sheet to {OUT}")


if __name__ == "__main__":
    asyncio.run(main())

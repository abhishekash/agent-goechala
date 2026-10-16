"""Compose a captioned MP4 from the real terminal UI's synthetic SVG captures.

Dependencies: Pillow, CairoSVG, imageio-ffmpeg.
Example:
    python scripts/compose_investigation_video.py \
        --captures /tmp/ao-capture --output media/investigation-demo.mp4
"""

import argparse
from io import BytesIO
from pathlib import Path

import cairosvg
from imageio_ffmpeg import write_frames
from PIL import Image, ImageDraw, ImageFont


WIDTH, HEIGHT, FPS = 1920, 1080, 15
BG = (8, 15, 25)
WHITE = (238, 244, 252)
MUTED = (155, 176, 198)
TEAL = (78, 232, 200)
BLUE = (112, 165, 237)
FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")


def font(size, mono=False, bold=False):
    family = "DejaVuSansMono" if mono else "DejaVuSans"
    suffix = "-Bold" if bold else ""
    return ImageFont.truetype(str(FONT_DIR / f"{family}{suffix}.ttf"), size)


def canvas():
    im = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(im)
    for y in range(HEIGHT):
        factor = y / HEIGHT
        draw.line((0, y, WIDTH, y), fill=(8, int(15 + factor * 7), int(25 + factor * 13)))
    draw.rectangle((0, 0, 13, HEIGHT), fill=TEAL)
    return im


def rounded_card(im, box, fill=(14, 24, 39), outline=(42, 65, 87), radius=22):
    draw = ImageDraw.Draw(im)
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=2)


def label(draw, xy, text, color=TEAL, size=24):
    draw.text(xy, text, font=font(size, mono=True, bold=True), fill=color)


def text_block(draw, xy, lines, size=32, color=WHITE, gap=14, mono=False):
    x, y = xy
    for line in lines:
        draw.text((x, y), line, font=font(size, mono=mono), fill=color)
        y += size + gap


def intro():
    im = canvas()
    draw = ImageDraw.Draw(im)
    label(draw, (110, 98), "AGENT OBSERVATORY  /  FIELD NOTE 01", size=28)
    draw.text((110, 260), "The agent hit an error.", font=font(76, bold=True), fill=WHITE)
    draw.text((110, 375), "What happened?", font=font(88, bold=True), fill=TEAL)
    text_block(draw, (115, 580), [
        "Follow one coding-agent session through its last action,",
        "a failed MCP call, and the linked execution trace."
    ], size=32, color=MUTED)
    rounded_card(im, (110, 830, 1810, 962), fill=(15, 34, 49))
    label(draw, (146, 864), "REAL PRODUCT UI  ·  SYNTHETIC SESSION DATA", size=30)
    return im


def ending():
    im = canvas()
    draw = ImageDraw.Draw(im)
    label(draw, (110, 102), "WHAT THE EVIDENCE SAYS", size=29)
    draw.text((110, 245), "The PR was not inspected.", font=font(67, bold=True), fill=WHITE)
    draw.text((110, 355), "GitHub returned 403: rate limit exceeded.",
              font=font(41), fill=TEAL)
    text_block(draw, (114, 520), [
        "Fix the GitHub access limit, then rerun the investigation.",
        "The checkout failure itself still needs diagnosis."
    ], size=30, color=MUTED)
    rounded_card(im, (110, 765, 1810, 938), fill=(16, 34, 49))
    label(draw, (145, 792), "TRY AGENT OBSERVATORY", size=25)
    draw.text((145, 845), "uvx --from agent-observatory-cli agent-observatory",
              font=font(34, mono=True, bold=True), fill=WHITE)
    draw.text((110, 996), "github.com/abhishekash/agent-observatory",
              font=font(26, mono=True), fill=BLUE)
    return im


def load_capture(path):
    svg = path.read_text().replace("Fira Code", "DejaVu Sans Mono").replace(
        "FiraCode", "DejaVu Sans Mono")
    png = cairosvg.svg2png(bytestring=svg.encode(), output_width=1600)
    return Image.open(BytesIO(png)).convert("RGB")


SCENES = [
    ("agents", 5, "01  FIND THE SESSION",
     "Codex · sample/payments-api · last action: get_pull_request",
     (50, 415, 1550, 445)),
    ("activity", 5, "02  SEE THE LAST ACTION",
     "The MCP request returned an error after 713 ms.",
     (50, 415, 930, 443)),
    ("mcp", 7, "03  READ THE MCP RESPONSE",
     "GitHub reported 403: API rate limit exceeded.",
     (970, 732, 1550, 844)),
    ("traces", 6, "04  FOLLOW THE TRACE",
     "The failed MCP span belongs to the same agent run.",
     (975, 323, 1550, 405)),
]


def ui_frame(capture, number, title, subtitle, highlight, progress):
    im = canvas()
    draw = ImageDraw.Draw(im)
    label(draw, (70, 42), "AGENT OBSERVATORY  /  SYNTHETIC CASE", size=22)
    draw.text((70, 84), title, font=font(47, bold=True), fill=WHITE)
    draw.text((72, 151), subtitle, font=font(25), fill=MUTED)
    rounded_card(im, (57, 209, 1863, 986), radius=19)

    # The UI capture is a real Textual SVG. Crop away the shell chrome to
    # enlarge the evidence panels for a shareable 1080p video.
    crop_top, crop_bottom = 280, 963
    source = capture.crop((0, crop_top, 1600, crop_bottom))
    zoom = 1.0 + 0.012 * progress
    target_width = int(1780 * zoom)
    target_height = int(source.height * target_width / source.width)
    source = source.resize((target_width, target_height), Image.Resampling.LANCZOS)
    visible = source.crop(((target_width - 1780) // 2, 0,
                           (target_width - 1780) // 2 + 1780, 750))
    im.paste(visible, (70, 222))
    draw = ImageDraw.Draw(im)
    hx1, hy1, hx2, hy2 = highlight
    scale = target_width / 1600
    offset = (target_width - 1780) // 2
    x1, x2 = 70 + hx1 * scale - offset, 70 + hx2 * scale - offset
    y1, y2 = 222 + (hy1 - crop_top) * scale, 222 + (hy2 - crop_top) * scale
    draw.rounded_rectangle((x1, y1, x2, y2), radius=9, outline=TEAL, width=4)
    # Thin progress track keeps the screen active without obscuring evidence.
    draw.rectangle((70, 1007, 1850, 1012), fill=(45, 65, 82))
    draw.rectangle((70, 1007, 70 + int(1780 * progress), 1012), fill=TEAL)
    draw.text((70, 1030), "Actual terminal UI · synthetic example · no real session data",
              font=font(20), fill=MUTED)
    draw.text((1705, 1030), f"{number:02d} / 04", font=font(21, mono=True), fill=TEAL)
    return im


def encode(captures, output, poster):
    output.parent.mkdir(parents=True, exist_ok=True)
    images = {name: load_capture(captures / f"{name}.svg") for name, *_ in SCENES}
    poster_image = intro()
    poster_image.save(poster, optimize=True)
    writer = write_frames(
        str(output), (WIDTH, HEIGHT), fps=FPS, codec="libx264", quality=7,
        pix_fmt_in="rgb24", macro_block_size=8,
        output_params=["-movflags", "+faststart"],
    )
    writer.send(None)
    try:
        for _ in range(3 * FPS):
            writer.send(poster_image.tobytes())
        for number, (name, seconds, title, subtitle, highlight) in enumerate(SCENES, 1):
            for frame in range(seconds * FPS):
                progress = frame / max(1, seconds * FPS - 1)
                rendered = ui_frame(images[name], number, title, subtitle,
                                    highlight, progress)
                writer.send(rendered.tobytes())
        final = ending()
        for _ in range(5 * FPS):
            writer.send(final.tobytes())
    finally:
        writer.close()
    print(f"Wrote {output} ({output.stat().st_size / 1024 / 1024:.1f} MiB)")
    print(f"Wrote {poster}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--captures", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--poster", type=Path)
    args = parser.parse_args()
    poster = args.poster or args.output.with_name(args.output.stem + "-poster.png")
    encode(args.captures, args.output, poster)

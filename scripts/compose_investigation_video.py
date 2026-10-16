"""Create a captioned, camera-moved product walkthrough from Textual SVG captures.

Dependencies: Pillow, CairoSVG, imageio-ffmpeg.
"""

import argparse
from io import BytesIO
from pathlib import Path

import cairosvg
from imageio_ffmpeg import write_frames
from PIL import Image, ImageDraw, ImageFont


WIDTH, HEIGHT, FPS = 1920, 1080, 24
BG = (8, 13, 21)
WHITE = (240, 246, 252)
MUTED = (194, 208, 224)
TEAL = (73, 229, 198)
FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
REF_WIDTH, REF_HEIGHT = 1600, 1015
CAPTURE_WIDTH, CAPTURE_HEIGHT = 1920, 1076
FIT_SCALE = HEIGHT / CAPTURE_HEIGHT
APP_X = round((WIDTH - CAPTURE_WIDTH * FIT_SCALE) / 2)


def font(size, mono=False, bold=False):
    name = "DejaVuSansMono" if mono else "DejaVuSans"
    if bold:
        name += "-Bold"
    return ImageFont.truetype(str(FONT_DIR / f"{name}.ttf"), size)


def capture(path):
    svg = path.read_text().replace("Fira Code", "DejaVu Sans Mono").replace(
        "FiraCode", "DejaVu Sans Mono")
    png = cairosvg.svg2png(bytestring=svg.encode(), output_width=CAPTURE_WIDTH)
    return Image.open(BytesIO(png)).convert("RGB")


def base_screen(source):
    frame = Image.new("RGB", (WIDTH, HEIGHT), BG)
    scaled = source.resize((round(source.width * FIT_SCALE), HEIGHT),
                           Image.Resampling.LANCZOS)
    frame.paste(scaled, (APP_X, 0))
    return frame


def map_point(x, y, zoom, crop_left, crop_top):
    source_x = x * CAPTURE_WIDTH / REF_WIDTH
    source_y = y * CAPTURE_HEIGHT / REF_HEIGHT
    return (round((APP_X + source_x * FIT_SCALE) * zoom - crop_left),
            round(source_y * FIT_SCALE * zoom - crop_top))


def map_box(box, zoom, crop_left, crop_top):
    x1, y1 = map_point(box[0], box[1], zoom, crop_left, crop_top)
    x2, y2 = map_point(box[2], box[3], zoom, crop_left, crop_top)
    return x1, y1, x2, y2


def caption_overlay(frame, headline, supporting=None, final=False):
    overlay = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    d.rectangle((0, 932, WIDTH, HEIGHT), fill=(5, 11, 18, 204))
    d.rectangle((67, 954, 72, 1042), fill=TEAL)
    d.text((98, 951), headline, font=font(32, bold=True), fill=WHITE)
    if supporting:
        d.text((100, 1000), supporting,
               font=font(22, mono=final), fill=TEAL if final else MUTED)
    return Image.alpha_composite(frame.convert("RGBA"), overlay).convert("RGB")


def draw_pointer(frame, point, click_strength=0):
    x, y = point
    d = ImageDraw.Draw(frame, "RGBA")
    if click_strength:
        radius = 19 + int((1 - click_strength) * 20)
        d.ellipse((x-radius, y-radius, x+radius, y+radius),
                  outline=(73, 229, 198, int(180 * click_strength)), width=3)
    # Small white mouse pointer with a dark edge remains visible over the UI.
    tip = (x, y)
    points = [tip, (x+2, y+25), (x+8, y+19), (x+15, y+31),
              (x+20, y+28), (x+13, y+16), (x+22, y+15)]
    d.polygon(points, fill=(248, 251, 255, 255), outline=(4, 8, 13, 255))
    d.line(points + [tip], fill=(4, 8, 13, 255), width=2, joint="curve")


def camera_frame(base, source_target, zoom, headline, supporting, pointer,
                 click_strength=0, highlight=None, final=False):
    target_x, target_y = source_target
    source_x = target_x * CAPTURE_WIDTH / REF_WIDTH
    source_y = target_y * CAPTURE_HEIGHT / REF_HEIGHT
    focus_x, focus_y = APP_X + source_x * FIT_SCALE, source_y * FIT_SCALE
    scaled_width, scaled_height = round(WIDTH * zoom), round(HEIGHT * zoom)
    crop_left = max(0, min(round(focus_x * zoom - WIDTH / 2), scaled_width - WIDTH))
    crop_top = max(0, min(round(focus_y * zoom - HEIGHT / 2), scaled_height - HEIGHT))
    scaled = base.resize((scaled_width, scaled_height), Image.Resampling.LANCZOS)
    frame = Image.new("RGB", (WIDTH, HEIGHT), BG)
    frame.paste(scaled, (-crop_left, -crop_top))
    if highlight:
        rect = map_box(highlight, zoom, crop_left, crop_top)
        d = ImageDraw.Draw(frame, "RGBA")
        d.rounded_rectangle(rect, radius=10, outline=(73, 229, 198, 235), width=4)
    if pointer:
        px, py = map_point(pointer[0], pointer[1], zoom, crop_left, crop_top)
        draw_pointer(frame, (px, py), click_strength)
    frame = caption_overlay(frame, headline, supporting, final)
    return frame


def smoothstep(value):
    value = max(0.0, min(1.0, value))
    return value * value * (3 - 2 * value)


def interpolate(a, b, t):
    return round(a + (b - a) * smoothstep(t))


SCENES = [
    # page, seconds, caption, supporting text, camera target, zoom,
    # pointer movements, click times, highlight rectangle
    ("agents", 5,
     "Three sessions. Which one needs attention?",
     "Find the project, status, update time, and latest action.",
     (800, 440), 1.07,
     [(0.08, 0.22, (267, 258))], [0.88], (58, 414, 1540, 445)),
    ("activity", 6,
     "The last action is a failed GitHub request.",
     "Activity keeps the error beside the session timeline.",
     (1170, 540), 1.10,
     [(0.08, 0.18, (450, 428)), (0.62, 0.75, (395, 258))],
     [0.24, 0.87], (978, 420, 1540, 853)),
    ("mcp", 8,
     "The MCP response explains why the investigation stopped.",
     "HTTP 403 · API rate limit exceeded · pull request not fetched",
     (1290, 738), 1.13,
     [(0.08, 0.17, (520, 596)), (0.30, 0.44, (1280, 790)),
      (0.68, 0.79, (1240, 913))],
     [0.25, 0.87], (974, 730, 1547, 855)),
    ("traces", 6,
     "Follow the failed span back to its agent run.",
     "The error is linked to the same trace and task.",
     (1270, 365), 1.12,
     [(0.08, 0.19, (1208, 384))], [0.26], (1002, 323, 1540, 400)),
]


def scene_frames(name, seconds, headline, supporting, target, max_zoom,
                 pointer_moves, click_times, highlight, source_images, previous_pointer,
                 final=False):
    base = base_screen(source_images[name])
    count = seconds * FPS
    pointer_start = previous_pointer or (45, 258)
    frames = []
    for i in range(count):
        t = i / max(1, count - 1)
        # Move the camera in, hold on the evidence, then ease out into the next page.
        zoom_in = smoothstep(t / 0.24)
        zoom_out = smoothstep((t - 0.80) / 0.20)
        zoom = 1.0 + (max_zoom - 1.0) * zoom_in * (1.0 - zoom_out)
        point = pointer_start
        for move_start, move_end, destination in pointer_moves:
            if t >= move_end:
                point = destination
            elif t > move_start:
                p = smoothstep((t - move_start) / (move_end - move_start))
                point = (interpolate(point[0], destination[0], p),
                         interpolate(point[1], destination[1], p))
                break
            else:
                break
        click = max((max(0.0, 1.0 - abs(t - at) / 0.045)
                     for at in click_times), default=0.0)
        frame = camera_frame(base, target, zoom, headline, supporting,
                             point, click, highlight, final)
        frames.append(frame)
    return frames, (pointer_moves[-1][2] if pointer_moves else pointer_start)


def intro_frame(base):
    return caption_overlay(base, "See what your coding agent did.",
                           "Session → MCP call → trace · local example data")


def outro_frames(base, seconds=4):
    headline = "The pull request was not inspected."
    supporting = "Fix the rate limit, rerun. Try: uvx --from agent-observatory-cli agent-observatory"
    frames = []
    for i in range(seconds * FPS):
        t = i / max(1, seconds * FPS - 1)
        zoom = 1.03 + 0.025 * smoothstep(t)
        frame = camera_frame(base, (1270, 365), zoom, headline,
                             supporting, (1208, 384), final=True)
        frames.append(frame)
    return frames


def encode(captures, output, poster):
    output.parent.mkdir(parents=True, exist_ok=True)
    images = {name: capture(captures / f"{name}.svg")
              for name in ("agents", "activity", "mcp", "traces")}
    opening_base = base_screen(images["agents"])
    poster_image = intro_frame(opening_base)
    poster.parent.mkdir(parents=True, exist_ok=True)
    poster_image.save(poster, optimize=True)

    writer = write_frames(
        str(output), (WIDTH, HEIGHT), fps=FPS, codec="libx264", quality=7,
        pix_fmt_in="rgb24", macro_block_size=8,
        output_params=["-movflags", "+faststart"],
    )
    writer.send(None)
    try:
        # Start directly on the product. The intro is only a brief lower third.
        for i in range(2 * FPS):
            t = i / max(1, 2 * FPS - 1)
            frame = caption_overlay(opening_base,
                "See what your coding agent did.",
                "Session → MCP call → trace · local example data")
            if i < 6:
                frame = Image.blend(opening_base, frame, i / 6)
            writer.send(frame.tobytes())

        prev_pointer = None
        last_page = "agents"
        for name, seconds, headline, supporting, target, zoom, moves, clicks, rect in SCENES:
            frames, prev_pointer = scene_frames(name, seconds, headline,
                supporting, target, zoom, moves, clicks, rect, images, prev_pointer)
            for frame in frames:
                writer.send(frame.tobytes())
            last_page = name
        for frame in outro_frames(base_screen(images[last_page])):
            writer.send(frame.tobytes())
    finally:
        writer.close()
    duration = 2 + sum(scene[1] for scene in SCENES) + 4
    print(f"Wrote {output} ({output.stat().st_size / 1024 / 1024:.1f} MiB, {duration}s)")
    print(f"Wrote {poster}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--captures", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--poster", type=Path)
    args = parser.parse_args()
    poster = args.poster or args.output.with_name(args.output.stem + "-poster.png")
    encode(args.captures, args.output, poster)

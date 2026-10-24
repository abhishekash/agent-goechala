"""Build the branded README walkthrough from actual Textual SVG captures.

Requires Pillow, CairoSVG, and imageio-ffmpeg. Frames are streamed to the encoder
so a full-HD video does not require keeping hundreds of images in memory. The
readme layout fills a 1600-pixel-wide frame with the terminal so the README GIF
stays legible in GitHub's content column.
"""
import argparse
from io import BytesIO
from pathlib import Path
import cairosvg
from imageio_ffmpeg import write_frames
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
WIDTH, HEIGHT, FPS = 1920, 1080, 24
BG, PANEL, WHITE = '#121018', '#191621', '#f6f0e8'
CYAN, GOLD, MUTED = '#c5adff', '#ffbd69', '#b9afc5'
FONT_DIR = Path('/usr/share/fonts/truetype/dejavu')
SCENES = [
    ('agents', 5, '01 / OVERVIEW', 'Every session, in one place.',
     'Codex, Claude Code, and OpenCode. Find the task and its latest recorded progress.', '1  Agents'),
    ('prompts', 5, '02 / CONTEXT', 'Read the prompts behind the work.',
     "Focus one agent, then read its user prompts. Keep each session's story together.", 's  Focus agent  /  Prompts'),
    ('mcp', 4, '03 / TOOLS', 'See which tool call failed.',
     'Observed MCP calls, their duration, and their outcome. Select a call to investigate.', '3  MCP calls'),
    ('mcp-detail', 6, '04 / EVIDENCE', 'Open the evidence.',
     'Arguments and indented JSON. This GitHub request hit an API rate limit.', 'e  Expand'),
    ('traces', 6, '05 / EXECUTION', 'Follow the call into its execution.',
     'The agent run, model call, and failed operation are connected in one tree.', 't  Linked trace'),
]

def font(size, bold=False, mono=False):
    family = 'DejaVuSansMono' if mono else 'DejaVuSans'
    return ImageFont.truetype(str(FONT_DIR / (family + ('-Bold' if bold else '') + '.ttf')), size)

def raster(path):
    svg = path.read_text().replace('Fira Code', 'DejaVu Sans Mono').replace('FiraCode', 'DejaVu Sans Mono')
    return Image.open(BytesIO(cairosvg.svg2png(bytestring=svg.encode(), output_width=1920))).convert('RGB')

def fitted(source, width, height):
    image = source.copy()
    image.thumbnail((width, height), Image.Resampling.LANCZOS)
    return image


def brand_icon(size):
    """Render the authoritative square SVG, never a potentially stale PNG export.

    Keep the tower's circular deck and transparent cutouts intact at the final
    display size, without a raster-resize or video-sized intermediate.
    """
    return Image.open(BytesIO(cairosvg.svg2png(
        url=str(ROOT / 'media/brand/icon.svg'),
        output_width=size, output_height=size,
    ))).convert('RGBA')

README_WIDTH = 1600
README_CAPTION = 190


def canvas_size(source, layout):
    if layout == 'readme':
        height = round(README_WIDTH * source.height / source.width) + README_CAPTION
        # H.264 needs even dimensions: pad the canvas, never stretch the logo.
        return README_WIDTH, height + height % 2
    return WIDTH, HEIGHT


def screen_frame(source, chapter, headline, supporting, keys, icon, progress=0,
                 layout='wide', size=(WIDTH, HEIGHT)):
    width, height = size
    frame = Image.new('RGB', (width, height), BG)
    draw = ImageDraw.Draw(frame)
    if layout == 'readme':
        image = fitted(source, width, height - README_CAPTION)
        frame.paste(image, (0, 0))
        top = image.height
        draw.text((48, top + 26), headline, font=font(40, bold=True), fill=WHITE)
        draw.text((50, top + 88), supporting, font=font(22), fill=MUTED)
        draw.text((50, top + 146), 'SYNTHETIC DATA / ACTUAL TERMINAL UI', font=font(15, mono=True), fill=MUTED)
        key_width = draw.textlength(keys, font=font(19, mono=True))
        draw.text((width - 50 - key_width, top + 146), keys, font=font(19, mono=True), fill=CYAN)
        chapter_width = draw.textlength(chapter, font=font(19, mono=True))
        draw.text((width - 50 - chapter_width, top + 30), chapter, font=font(19, mono=True), fill=GOLD)
        return frame
    frame.paste(icon, (57, 22), icon)
    draw.text((136, 27), 'Agenturm', font=font(28, bold=True), fill=WHITE)
    draw.text((138, 65), 'A clear view of every agent.', font=font(16), fill=MUTED)
    draw.text((1450, 38), chapter, font=font(19, mono=True), fill=GOLD)
    draw.line((64, 106, 1856, 106), fill='#51475f', width=1)
    draw.line((64, 106, 64 + int(1792 * progress), 106), fill=GOLD, width=3)
    image = fitted(source, 1792, 754)
    x, y = (WIDTH-image.width)//2, 127 + (754-image.height)//2
    draw.rounded_rectangle((x-2, y-2, x+image.width+2, y+image.height+2), radius=12, fill=PANEL, outline='#51475f', width=2)
    frame.paste(image, (x, y))
    draw.text((66, 922), headline, font=font(35, bold=True), fill=WHITE)
    draw.text((68, 977), supporting, font=font(21), fill=MUTED)
    draw.text((68, 1030), 'SYNTHETIC DATA / ACTUAL TERMINAL UI', font=font(14, mono=True), fill=MUTED)
    key_width = draw.textlength(keys, font=font(18, mono=True))
    draw.text((1852-key_width, 1030), keys, font=font(18, mono=True), fill=CYAN)
    return frame

def outro(icon, layout='wide', size=(WIDTH, HEIGHT)):
    width, height = size
    frame = Image.new('RGB', (width, height), BG)
    if layout == 'readme':
        large = brand_icon(240)
        frame.paste(large, ((width-240)//2, 220), large)
        draw=ImageDraw.Draw(frame)
        def centered(text,y,typeface,fill):
            width_=draw.textlength(text,font=typeface)
            draw.text(((width-width_)/2,y),text,font=typeface,fill=fill)
        centered('Agenturm', 510, font(64,bold=True), WHITE)
        centered('A clear view of every agent.', 610, font(30), CYAN)
        centered('agenturm', 720, font(27,mono=True), GOLD)
        centered('Local agent observability. Right in your terminal.', 830, font(23), MUTED)
        return frame
    large = brand_icon(320)
    frame.paste(large, (800,100), large)
    draw=ImageDraw.Draw(frame)
    def centered(text,y,typeface,fill):
        width=draw.textlength(text,font=typeface)
        draw.text(((WIDTH-width)/2,y),text,font=typeface,fill=fill)
    centered('Agenturm', 450, font(70,bold=True), WHITE)
    centered('A clear view of every agent.', 553, font(34), CYAN)
    centered('agenturm', 694, font(30,mono=True), GOLD)
    centered('Local agent observability. Right in your terminal.', 824, font(25), MUTED)
    return frame

def encode(captures, output, poster, layout='wide'):
    output.parent.mkdir(parents=True,exist_ok=True)
    icon=brand_icon(68)
    images={scene[0]:raster(captures/f'{scene[0]}.svg') for scene in SCENES}
    size=canvas_size(images[SCENES[0][0]], layout)
    if layout == 'wide':
        screenshots=ROOT/'media/screenshots'
        screenshots.mkdir(parents=True,exist_ok=True)
        for name,source in images.items():
            source.save(screenshots/f'{name}.png',optimize=True)
        raster(captures/'compact.svg').save(screenshots/'compact.png',optimize=True)
    first=SCENES[0]
    poster.parent.mkdir(parents=True,exist_ok=True)
    screen_frame(images[first[0]],*first[2:],icon,layout=layout,size=size).save(poster,optimize=True)
    writer=write_frames(str(output),size,fps=FPS,codec='libx264',quality=7,
        pix_fmt_in='rgb24',pix_fmt_out='yuv420p',macro_block_size=2,
        output_params=['-movflags','+faststart','-metadata','title=Agenturm — a clear view of every agent.'])
    writer.send(None)
    total=sum(scene[1] for scene in SCENES)+3
    elapsed=0
    previous=None
    try:
        for name,seconds,chapter,headline,supporting,keys in SCENES:
            for i in range(seconds*FPS):
                frame=screen_frame(images[name],chapter,headline,supporting,keys,icon,
                                   (elapsed+i/FPS)/total,layout=layout,size=size)
                if previous is not None and i<8:
                    frame=Image.blend(previous,frame,(i+1)/8)
                writer.send(frame.tobytes())
            previous=frame
            elapsed+=seconds
        ending=outro(icon,layout,size)
        for i in range(3*FPS):
            frame=Image.blend(previous,ending,(i+1)/12) if i<12 else ending
            writer.send(frame.tobytes())
    finally:
        writer.close()
    print(f'Wrote {output}: {total}s, {size[0]}x{size[1]}, {output.stat().st_size/1024/1024:.1f} MiB')
    print(f'Poster: {poster}' + (f'; six terminal screenshots: {screenshots}' if layout == 'wide' else ''))

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--captures',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--poster',type=Path)
    parser.add_argument('--layout', choices=('wide', 'readme'), default='wide',
                        help='wide: 1920x1080 walkthrough video; readme: full-width terminal frame for the README GIF')
    args=parser.parse_args()
    encode(args.captures,args.output,args.poster or args.output.with_name(args.output.stem+'-poster.png'),args.layout)

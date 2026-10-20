"""Build the branded README walkthrough from actual Textual SVG captures.

Requires Pillow, CairoSVG, and imageio-ffmpeg. Frames are streamed to the encoder
so a full-HD video does not require keeping hundreds of images in memory.
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

def screen_frame(source, chapter, headline, supporting, keys, icon, progress=0):
    frame = Image.new('RGB', (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(frame)
    frame.paste(icon, (57, 22), icon)
    draw.text((136, 27), 'AGENT GOECHALA', font=font(28, bold=True), fill=WHITE)
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

def outro(icon):
    frame = Image.new('RGB', (WIDTH, HEIGHT), BG)
    large = Image.open(ROOT/'media/brand/icon-512.png').convert('RGBA').resize((320,320),Image.Resampling.LANCZOS)
    frame.paste(large, (800,100), large)
    draw=ImageDraw.Draw(frame)
    def centered(text,y,typeface,fill):
        width=draw.textlength(text,font=typeface)
        draw.text(((WIDTH-width)/2,y),text,font=typeface,fill=fill)
    centered('Agent Goechala', 450, font(70,bold=True), WHITE)
    centered('A clear view of every agent.', 553, font(34), CYAN)
    centered('agent-goechala', 694, font(30,mono=True), GOLD)
    centered('Local agent observability. Right in your terminal.', 824, font(25), MUTED)
    return frame

def encode(captures, output, poster):
    output.parent.mkdir(parents=True,exist_ok=True)
    icon=Image.open(ROOT/'media/brand/icon-128.png').convert('RGBA').resize((68,68),Image.Resampling.LANCZOS)
    images={scene[0]:raster(captures/f'{scene[0]}.svg') for scene in SCENES}
    screenshots=ROOT/'media/screenshots'
    screenshots.mkdir(parents=True,exist_ok=True)
    for name,source in images.items():
        source.save(screenshots/f'{name}.png',optimize=True)
    raster(captures/'compact.svg').save(screenshots/'compact.png',optimize=True)
    first=SCENES[0]
    poster.parent.mkdir(parents=True,exist_ok=True)
    screen_frame(images[first[0]],*first[2:],icon).save(poster,optimize=True)
    writer=write_frames(str(output),(WIDTH,HEIGHT),fps=FPS,codec='libx264',quality=7,
        pix_fmt_in='rgb24',pix_fmt_out='yuv420p',macro_block_size=8,
        output_params=['-movflags','+faststart','-metadata','title=Agent Goechala — a clear view of every agent.'])
    writer.send(None)
    total=sum(scene[1] for scene in SCENES)+3
    elapsed=0
    previous=None
    try:
        for name,seconds,chapter,headline,supporting,keys in SCENES:
            for i in range(seconds*FPS):
                frame=screen_frame(images[name],chapter,headline,supporting,keys,icon,(elapsed+i/FPS)/total)
                if previous is not None and i<8:
                    frame=Image.blend(previous,frame,(i+1)/8)
                writer.send(frame.tobytes())
            previous=frame
            elapsed+=seconds
        ending=outro(icon)
        for i in range(3*FPS):
            frame=Image.blend(previous,ending,(i+1)/12) if i<12 else ending
            writer.send(frame.tobytes())
    finally:
        writer.close()
    print(f'Wrote {output}: {total}s, 1920x1080, {output.stat().st_size/1024/1024:.1f} MiB')
    print(f'Poster: {poster}; six terminal screenshots: {screenshots}')

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--captures',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--poster',type=Path)
    args=parser.parse_args()
    encode(args.captures,args.output,args.poster or args.output.with_name(args.output.stem+'-poster.png'))

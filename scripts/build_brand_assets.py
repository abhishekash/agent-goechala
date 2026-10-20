"""Build Agent Goechala's editable vector identity and raster exports.

Requires CairoSVG. The mark is a mountain pass: two ridges, an open saddle,
and a golden route through it. All assets share the terminal's Alpine Ember
palette. No external images or fonts are downloaded.
"""

from pathlib import Path

import cairosvg


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "media" / "brand"
NAVY = "#121018"
CYAN = "#c5adff"
GOLD = "#ffbd69"
WHITE = "#f6f0e8"
MUTED = "#b9afc5"


def mark(background=True):
    plate = f'<rect x="16" y="16" width="480" height="480" rx="112" fill="{NAVY}" stroke="#51475f" stroke-width="2"/>' if background else ""
    return f"""<defs>
  <linearGradient id="warm-ridge" x1="0" y1="0" x2=".7" y2="1"><stop stop-color="#ffe6bd"/><stop offset=".5" stop-color="{GOLD}"/><stop offset="1" stop-color="#d87946"/></linearGradient>
  <linearGradient id="cool-ridge" x1="0" y1="0" x2=".6" y2="1"><stop stop-color="#eee4ff"/><stop offset=".5" stop-color="{CYAN}"/><stop offset="1" stop-color="#8262bc"/></linearGradient>
</defs>
{plate}
  <circle cx="265" cy="176" r="26" fill="{GOLD}"/>
  <path d="M64 356 177 122 258 271 231 319 280 356Z" fill="url(#warm-ridge)"/>
  <path d="m177 122 24 143 30 54 27-48Z" fill="#a34e36"/>
  <path d="M258 271 345 161 449 356H280l-49-37Z" fill="url(#cool-ridge)"/>
  <path d="m345 161-19 128-46 67h169Z" fill="#7960a4"/>
  <path d="m177 122-37 77 37-22 28 20Z" fill="#fff0d6"/>
  <path d="m345 161-36 46 36-13 25 10Z" fill="#f0e8ff"/>
  <path d="m258 271-24 49 47 37-56 57" fill="none" stroke="{NAVY}" stroke-width="34" stroke-linejoin="round"/>
  <path d="m258 271-24 49 47 37-56 57" fill="none" stroke="{WHITE}" stroke-width="13" stroke-linecap="round" stroke-linejoin="round"/>
"""


def svg(width, height, body, title):
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">
<title id="title">{title}</title>
<desc id="desc">Amber and lavender mountain ridges frame an open pass. A bright winding trail leads through the saddle toward the rising sun.</desc>
{body}
</svg>'''


def build():
    OUT.mkdir(parents=True, exist_ok=True)
    icon = svg(512, 512, mark(), "Agent Goechala — the mountain pass")
    (OUT / "icon.svg").write_text(icon)
    (OUT / "icon-transparent.svg").write_text(svg(512, 512, mark(False), "Agent Goechala pass mark"))
    for size in (32, 64, 128, 256, 512, 1024):
        cairosvg.svg2png(bytestring=icon.encode(), write_to=str(OUT / f"icon-{size}.png"), output_width=size, output_height=size)

    wordmark = svg(1160, 240, f'''
<g transform="translate(8 8) scale(.4375)">{mark(False)}</g>
<text x="268" y="92" fill="{CYAN}" font-family="DejaVu Sans Mono,monospace" font-size="25" letter-spacing="8">AGENT</text>
<text x="265" y="170" fill="{WHITE}" font-family="DejaVu Sans,Arial,sans-serif" font-weight="bold" font-size="76" letter-spacing="2">GOECHALA</text>
''', "Agent Goechala wordmark")
    (OUT / "logo.svg").write_text(wordmark)

    hero = svg(1280, 420, f'''
<defs>
  <linearGradient id="sky" x2="1" y2="1"><stop stop-color="#30223d"/><stop offset="1" stop-color="{NAVY}"/></linearGradient>
  <radialGradient id="dawn"><stop stop-color="{GOLD}" stop-opacity=".12"/><stop offset="1" stop-color="{GOLD}" stop-opacity="0"/></radialGradient>
</defs>
<rect width="1280" height="420" rx="28" fill="url(#sky)"/>
<circle cx="1060" cy="170" r="230" fill="url(#dawn)"/>
<path d="m760 420 210-270 94 122 101-150 115 154M810 420l160-212 94 122 101-150 115 154M890 420l80-95 94 63 101-110 115 112" fill="none" stroke="#67516d" stroke-width="1" opacity=".6"/>
<path d="M64 55h30" stroke="{GOLD}" stroke-width="3"/>
<text x="106" y="61" fill="{MUTED}" font-family="DejaVu Sans Mono,monospace" font-size="13" letter-spacing="3">LOCAL. TERMINAL. OPEN SOURCE.</text>
<text x="62" y="140" fill="{CYAN}" font-family="DejaVu Sans Mono,monospace" font-size="25" letter-spacing="7">AGENT</text>
<text x="58" y="227" fill="{WHITE}" font-family="DejaVu Sans,Arial,sans-serif" font-weight="bold" font-size="84" letter-spacing="1">GOECHALA</text>
<text x="64" y="282" fill="{WHITE}" font-family="DejaVu Sans,Arial,sans-serif" font-size="28">A clear view of every agent.</text>
<text x="65" y="354" fill="{MUTED}" font-family="DejaVu Sans Mono,monospace" font-size="15">CODEX  /  CLAUDE CODE  /  OPENCODE</text>
<g transform="translate(913 50) scale(.65)">{mark(False)}</g>
<rect x=".5" y=".5" width="1279" height="419" rx="28" fill="none" stroke="#51475f"/>
''', "Agent Goechala — a clear view of every agent")
    (OUT / "hero.svg").write_text(hero)
    cairosvg.svg2png(bytestring=hero.encode(), write_to=str(OUT / "hero.png"), output_width=2560, output_height=840)
    social = svg(1280, 640, f'''
<rect width="1280" height="640" fill="{NAVY}"/>
<g transform="translate(470 22) scale(.66)">{mark(False)}</g>
<text x="640" y="428" fill="{WHITE}" text-anchor="middle" font-family="DejaVu Sans,Arial,sans-serif" font-weight="bold" font-size="62">Agent Goechala</text>
<text x="640" y="488" fill="{CYAN}" text-anchor="middle" font-family="DejaVu Sans,Arial,sans-serif" font-size="28">A clear view of every agent.</text>
<text x="640" y="568" fill="{MUTED}" text-anchor="middle" font-family="DejaVu Sans Mono,monospace" font-size="17">LOCAL OBSERVABILITY  /  RIGHT IN YOUR TERMINAL</text>
''', "Agent Goechala social card")
    (OUT / "social-preview.svg").write_text(social)
    cairosvg.svg2png(bytestring=social.encode(), write_to=str(OUT / "social-preview.png"))
    print(f"Built vector originals, six icon sizes, README hero, and social preview in {OUT}")


if __name__ == "__main__":
    build()

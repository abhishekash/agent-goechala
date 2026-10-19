"""Build Agent Goechala's editable vector identity and raster exports.

Requires CairoSVG. The mark is a mountain pass: two ridges, an open saddle,
and a golden route through it. All assets share the terminal's Alpine Night
palette. No external images or fonts are downloaded.
"""

from pathlib import Path

import cairosvg


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "media" / "brand"
NAVY = "#08121f"
CYAN = "#73dce8"
GOLD = "#ffd08a"
WHITE = "#f2f7fa"
MUTED = "#9aafc3"


def mark(background=True):
    plate = f'<rect x="16" y="16" width="480" height="480" rx="108" fill="{NAVY}"/>' if background else ""
    return f"""{plate}
  <path d="M80 354 182 156 256 278 334 194 432 354Z" fill="#173148"/>
  <path d="m80 354 102-198 74 122 78-84 98 160" fill="none" stroke="{CYAN}" stroke-width="21" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="m182 156 17 89 57 33M334 194l-16 91-62-7" fill="none" stroke="#3b7b91" stroke-width="10" stroke-linejoin="round"/>
  <circle cx="260" cy="178" r="21" fill="{GOLD}"/>
  <path d="m224 412 66-48-48-39 18-47" fill="none" stroke="{GOLD}" stroke-width="15" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="224" cy="412" r="8" fill="{WHITE}"/>
"""


def svg(width, height, body, title):
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">
<title id="title">{title}</title>
<desc id="desc">Two cyan mountain ridges frame a pass. A golden path leads through the saddle toward a warm sunrise.</desc>
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
  <linearGradient id="sky" x2="1" y2="1"><stop stop-color="#102b40"/><stop offset="1" stop-color="{NAVY}"/></linearGradient>
  <radialGradient id="dawn"><stop stop-color="{GOLD}" stop-opacity=".12"/><stop offset="1" stop-color="{GOLD}" stop-opacity="0"/></radialGradient>
</defs>
<rect width="1280" height="420" rx="28" fill="url(#sky)"/>
<circle cx="1060" cy="170" r="230" fill="url(#dawn)"/>
<path d="m760 420 210-270 94 122 101-150 115 154M810 420l160-212 94 122 101-150 115 154M890 420l80-95 94 63 101-110 115 112" fill="none" stroke="#28465b" stroke-width="1" opacity=".6"/>
<path d="M64 55h30" stroke="{GOLD}" stroke-width="3"/>
<text x="106" y="61" fill="{MUTED}" font-family="DejaVu Sans Mono,monospace" font-size="13" letter-spacing="3">LOCAL. TERMINAL. OPEN SOURCE.</text>
<text x="62" y="140" fill="{CYAN}" font-family="DejaVu Sans Mono,monospace" font-size="25" letter-spacing="7">AGENT</text>
<text x="58" y="227" fill="{WHITE}" font-family="DejaVu Sans,Arial,sans-serif" font-weight="bold" font-size="84" letter-spacing="1">GOECHALA</text>
<text x="64" y="282" fill="{WHITE}" font-family="DejaVu Sans,Arial,sans-serif" font-size="28">A clear view of every agent.</text>
<text x="65" y="354" fill="{MUTED}" font-family="DejaVu Sans Mono,monospace" font-size="15">CODEX  /  CLAUDE CODE  /  OPENCODE</text>
<g transform="translate(913 50) scale(.65)">{mark(False)}</g>
<rect x=".5" y=".5" width="1279" height="419" rx="28" fill="none" stroke="#304b63"/>
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

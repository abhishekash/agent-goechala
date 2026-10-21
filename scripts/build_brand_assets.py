"""Build Agenturm's editable vector identity and raster exports.

Requires CairoSVG. The mark is a watchtower: a clear vantage point over agent
activity. All assets share the terminal's Alpine Ember palette. No external
images or fonts are downloaded.
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
    return f"""
{plate}
  <path d="M144 112h48v48h40v-48h48v48h40v-48h48v112h-40l16 144h24v40H144v-40h24l16-144h-40Z
           M240 264v56h32v-56a16 16 0 0 0-32 0Z" fill="{GOLD}" fill-rule="evenodd"/>
"""


def svg(width, height, body, title):
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">
<title id="title">{title}</title>
<desc id="desc">A geometric amber tower with three battlements and a single observation window.</desc>
{body}
</svg>'''


def build():
    OUT.mkdir(parents=True, exist_ok=True)
    icon = svg(512, 512, mark(), "Agenturm tower mark")
    (OUT / "icon.svg").write_text(icon)
    (OUT / "icon-transparent.svg").write_text(svg(512, 512, mark(False), "Agenturm tower mark"))
    for size in (32, 64, 128, 256, 512, 1024):
        cairosvg.svg2png(bytestring=icon.encode(), write_to=str(OUT / f"icon-{size}.png"), output_width=size, output_height=size)

    wordmark = svg(1160, 240, f'''
<g transform="translate(8 8) scale(.4375)">{mark(False)}</g>
<text x="268" y="144" fill="{WHITE}" font-family="DejaVu Sans,Arial,sans-serif" font-weight="bold" font-size="78" letter-spacing="1">Agenturm</text>
''', "Agenturm wordmark")
    (OUT / "logo.svg").write_text(wordmark)

    hero = svg(1280, 420, f'''
<defs>
  <linearGradient id="sky" x2="1" y2="1"><stop stop-color="#30223d"/><stop offset="1" stop-color="{NAVY}"/></linearGradient>
  <radialGradient id="dawn"><stop stop-color="{GOLD}" stop-opacity=".12"/><stop offset="1" stop-color="{GOLD}" stop-opacity="0"/></radialGradient>
</defs>
<rect width="1280" height="420" rx="28" fill="url(#sky)"/>
<circle cx="1060" cy="170" r="230" fill="url(#dawn)"/>
<path d="M1114 72V350M1154 98V350M1194 72V350M1082 130h144M1082 205h144M1082 280h144" fill="none" stroke="#67516d" stroke-width="2" opacity=".6"/>
<path d="M64 55h30" stroke="{GOLD}" stroke-width="3"/>
<text x="106" y="61" fill="{MUTED}" font-family="DejaVu Sans Mono,monospace" font-size="13" letter-spacing="3">LOCAL. TERMINAL. OPEN SOURCE.</text>
<text x="58" y="181" fill="{WHITE}" font-family="DejaVu Sans,Arial,sans-serif" font-weight="bold" font-size="84" letter-spacing="1">AGENTURM</text>
<text x="64" y="282" fill="{WHITE}" font-family="DejaVu Sans,Arial,sans-serif" font-size="28">A clear view of every agent.</text>
<text x="65" y="354" fill="{MUTED}" font-family="DejaVu Sans Mono,monospace" font-size="15">CODEX  /  CLAUDE CODE  /  OPENCODE</text>
<g transform="translate(913 50) scale(.65)">{mark(False)}</g>
<rect x=".5" y=".5" width="1279" height="419" rx="28" fill="none" stroke="#51475f"/>
''', "Agenturm — a clear view of every agent")
    (OUT / "hero.svg").write_text(hero)
    cairosvg.svg2png(bytestring=hero.encode(), write_to=str(OUT / "hero.png"), output_width=2560, output_height=840)
    social = svg(1280, 640, f'''
<rect width="1280" height="640" fill="{NAVY}"/>
<g transform="translate(470 22) scale(.66)">{mark(False)}</g>
<text x="640" y="428" fill="{WHITE}" text-anchor="middle" font-family="DejaVu Sans,Arial,sans-serif" font-weight="bold" font-size="62">Agenturm</text>
<text x="640" y="488" fill="{CYAN}" text-anchor="middle" font-family="DejaVu Sans,Arial,sans-serif" font-size="28">A clear view of every agent.</text>
<text x="640" y="568" fill="{MUTED}" text-anchor="middle" font-family="DejaVu Sans Mono,monospace" font-size="17">LOCAL OBSERVABILITY  /  RIGHT IN YOUR TERMINAL</text>
''', "Agenturm social card")
    (OUT / "social-preview.svg").write_text(social)
    cairosvg.svg2png(bytestring=social.encode(), write_to=str(OUT / "social-preview.png"))
    print(f"Built vector originals, six icon sizes, README hero, and social preview in {OUT}")


if __name__ == "__main__":
    build()

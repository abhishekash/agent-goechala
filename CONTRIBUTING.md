# Contributing

Thanks for helping improve Agenturm. For bugs and feature ideas, open a GitHub issue with the expected behavior and a minimal reproduction when possible.

## Local development

- Python 3.11 or newer
- Node.js 18 or newer for the JavaScript client
- uv recommended for isolated CLI installation

```bash
uv tool install --editable .
python -m unittest discover -s tests -v
node --check clients/javascript/index.mjs
```

Keep changes focused, document user-facing options, and avoid committing local databases, session captures, credentials, or real customer telemetry. Add or update tests for behavioral changes. CI runs the Python suite on Python 3.11–3.14 and checks the JavaScript module syntax on Node.js 22 and 24.

## Terminal preview

The README walkthrough is reproducible and uses synthetic records only:

```bash
uv run --with cairosvg --with pillow python scripts/render_terminal_demo.py --snapshots /tmp/observatory-preview
```

This writes the five-view GIF to `media/terminal-demo.gif` and optional wide/compact PNGs to the supplied directory. It uses Textual's headless renderer and never connects to a receiver. Review the previews when changing layout; keep real telemetry out of demo media.

## Brand and demo assets

The SVG tower identity, color palette, and raster exports are documented in [docs/branding.md](docs/branding.md). Reproduce the README video and keyboard-driven TUI captures with [docs/investigation-video.md](docs/investigation-video.md). Use only synthetic records for public media.

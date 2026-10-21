# Agenturm walkthrough

[Watch the README animation](../media/agenturm-demo.gif),
[open the current terminal overview](../media/agenturm-terminal.png),
[inspect the failed MCP call](../media/screenshots/mcp-detail.png), or
[see the compact layout](../media/screenshots/compact.png).

This walkthrough uses **actual Textual screenshots of Agenturm**, assembled
into a reproducible investigation sequence. The examples are synthetic. The capture uses an in-memory
feed: it does not read personal prompts, local agent logs, a receiver, or a real
database. There is no paid model call or voiceover.

## Investigation sequence

| Time | On screen | Action |
| --- | --- | --- |
| 0–5s | Every session, in one place. | Agents overview with Codex, OpenCode, and Claude Code sessions |
| 5–10s | Read the prompts behind the work. | Focus the checkout agent and enable the Prompts filter |
| 10–14s | See which tool call failed. | Inspect the observed MCP call list |
| 14–20s | Open the evidence. | Expand the failed GitHub request and its indented JSON response |
| 20–26s | Follow the call into its execution. | Follow the MCP operation to its linked execution tree |
| 26–29s | Agenturm. A clear view of every agent. | Tower mark and the new command |

The observed request returned HTTP 403, `API rate limit exceeded`. The demo does
not claim that checkout was fixed: the agent has not yet fetched the pull request.

## Rebuild

From the repository root, with [uv](https://docs.astral.sh/uv/) installed:

```bash
uv run --with cairosvg python scripts/build_brand_assets.py
uv run --with cairosvg python scripts/capture_investigation_video.py \
  --output /tmp/opencode/agenturm-capture \
  --screenshots media/screenshots --readme-image media/agenturm-terminal.png
```

Render the 29-second walkthrough from those 148-column captures:

```bash
uv run --with cairosvg --with pillow --with imageio-ffmpeg \
  python scripts/compose_investigation_video.py \
  --captures /tmp/opencode/agenturm-capture --output /tmp/opencode/agenturm-demo.mp4
```

Build the README animation from a 100-column terminal so the text stays
legible in GitHub's content column:

```bash
uv run --with cairosvg python scripts/capture_investigation_video.py \
  --size 100x42 --output /tmp/opencode/agenturm-readme
uv run --with cairosvg --with pillow --with imageio-ffmpeg \
  python scripts/compose_investigation_video.py --layout readme \
  --captures /tmp/opencode/agenturm-readme --output /tmp/opencode/agenturm-readme.mp4
uv run --with imageio-ffmpeg python scripts/make_readme_preview.py \
  --video /tmp/opencode/agenturm-readme.mp4 --output media/agenturm-demo.gif
```

The preview uses the video's native resolution at 12 fps with a 256-colour
palette. Use `--width` or `--fps` to trade resolution for file size.

The capture script drives the actual TUI through its keyboard actions and
Prompts button, and also captures an 80 × 24 terminal. The composer streams
1920 × 1080 frames for the wide layout and a 1600-pixel-wide frame for the
README layout, rather than retaining a video in memory. Its source screens are
exported to `media/screenshots/`. Fonts use DejaVu Sans and DejaVu
Sans Mono; install those fonts before reproducing the raster/video assets.

The source SVG identity lives in `media/brand/`. Its Berlin-inspired tower,
purple sphere, central hand lens, and Alpine Ember palette are described in
[branding.md](branding.md).

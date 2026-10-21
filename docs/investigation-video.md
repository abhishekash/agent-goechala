# Agenturm walkthrough

[Watch the 29-second 1080p MP4](../media/goechala-demo.mp4),
[view the animated README preview](../media/goechala-demo-preview.gif), or
[open the poster](../media/goechala-demo-poster.png).

This walkthrough uses **actual Textual screenshots of Agenturm**, assembled
into a captioned video. The examples are synthetic. The capture uses an in-memory
feed: it does not read personal prompts, local agent logs, a receiver, or a real
database. There is no paid model call or voiceover.

## Transcript

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
uv run python scripts/capture_investigation_video.py --output /tmp/goechala-capture
uv run --with cairosvg --with pillow --with imageio-ffmpeg \
  python scripts/compose_investigation_video.py \
  --captures /tmp/goechala-capture --output media/goechala-demo.mp4
uv run --with imageio-ffmpeg python scripts/make_readme_preview.py \
  --video media/goechala-demo.mp4 --output media/goechala-demo-preview.gif
```

The capture script drives the actual TUI through its keyboard actions and
Prompts button, and also captures an 80 × 24 terminal. The composer streams
1920 × 1080 frames to H.264 rather than retaining a video in memory. Its source
screens are exported to `media/screenshots/`. Fonts use DejaVu Sans and DejaVu
Sans Mono; install those fonts before reproducing the raster/video assets.

The source SVG identity lives in `media/brand/`. Its two ridges, open pass,
bright trail, and Alpine Ember palette are described in [branding.md](branding.md).

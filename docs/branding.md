# Agent Goechala identity

**A clear view of every agent.**

Goechala is a mountain pass. The logo shows two mountain ridges framing an
open saddle, with a golden route leading through it. The subject is the pass
and the way through: an overview that helps you find the next useful detail.

## Assets

| Asset | Use |
| --- | --- |
| [Pass mark](../media/brand/icon.svg) | App icon, avatar, square placements |
| [Transparent mark](../media/brand/icon-transparent.svg) | Dark backgrounds and overlays |
| [Wordmark](../media/brand/logo.svg) | Dark headers and presentations |
| [README hero](../media/brand/hero.svg) | Repository homepage |
| [Social preview](../media/brand/social-preview.png) | GitHub repository social image, 1280 × 640 |

PNG icon exports are available at 32, 64, 128, 256, 512, and 1024 pixels.
The SVGs are the editable source. Keep the mark's proportions and leave clear
space around it. At small sizes, use the mark without a wordmark.

## Alpine Night

| Role | Color | Use |
| --- | --- | --- |
| Midnight | `#08121f` | Canvas |
| Shelter | `#0d1b2a` | Panels |
| Ridge | `#173148` | Raised surfaces and table headers |
| Glacier | `#73dce8` | Active navigation, focus, mountain outlines |
| Sunrise | `#ffd08a` | Route through the pass, attention and current work |
| Frost | `#f2f7fa` | Primary text |
| Slate | `#9aafc3` | Supporting text |
| Error | `#ff9d91` | Failures, accompanied by an explicit status label |

Use color to support text, never as the sole status indicator. Focus remains
visible through borders and selected rows. The identity is static: no flashing
logo or decorative loading animation. The terminal uses a text version of the
same pass mark (`╱╲_╱╲`) so it works without image protocols or emoji fonts.

## Naming and compatibility

- Product: **Agent Goechala**
- Repository, Python distribution, and command: **agent-goechala**
- JavaScript client: **@agent-goechala/client**
- Former name: Agent Observatory

The `observatory` Python import, `OBSERVATORY_*` settings, telemetry attribute
names, and the default `observatory.db` filename remain compatible. Existing
saved history is reused. The old `agent-observatory` command remains an alias.
Package publication and PyPI's Trusted Publisher registration are separate from
the source rename; see [publishing](publishing.md).

## Rebuild

```bash
uv run --with cairosvg python scripts/build_brand_assets.py
```

See [the demo production guide](investigation-video.md) to reproduce the README
video, animated preview, and actual terminal screenshots.

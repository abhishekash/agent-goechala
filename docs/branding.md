# Agent Goechala identity

**A clear view of every agent.**

Goechala is a mountain pass. The logo shows two faceted mountain ridges framing an
open saddle, with a bright trail leading through it. The subject is the pass
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

## Alpine Ember

| Role | Color | Use |
| --- | --- | --- |
| Charcoal | `#121018` | Canvas |
| Night | `#191621` | Panels |
| Violet grey | `#242030` | Raised surfaces and controls |
| Amber | `#ffbd69` | Active navigation, keyboard focus, current work |
| Lavender | `#c5adff` | Detail headings, tool inventory, mountain face |
| Mint | `#86e3b5` | Successful calls and healthy activity |
| Ice | `#86d9ed` | Usage values |
| Frost | `#f6f0e8` | Primary text and the trail through the pass |
| Slate | `#b9afc5` | Supporting text |
| Rose | `#ff8799` | Failures, accompanied by an explicit status label |

Use color to support text, never as the sole status indicator. Focus remains
visible through borders and selected rows. The identity is static: no flashing
logo or decorative loading animation. The terminal uses a text version of the
same pass mark, with two colored ridges and a contrasting saddle. Wide terminals
use a two-line block silhouette; compact terminals use `◢◣_◢◣`. Neither needs
image protocols, emoji rendering, or a Nerd Font.

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

## Design references

The presentation takes cues from [Lazygit's explicit focus styling](https://github.com/jesseduffield/lazygit/blob/master/docs/Config.md),
[btop's use of distinct panel accents](https://github.com/aristocratos/btop), and
[Glow's typography and color hierarchy](https://github.com/charmbracelet/glow).
The pass artwork is original. Amber marks the active view; supporting labels
remain neutral, and status always has a text label as well as color.

Primary text, supporting text, status accents, and active-tab text are checked
against their backgrounds for at least 4.5:1 contrast. Inactive decorative borders
are intentionally quieter; focused borders use the bright amber accent.

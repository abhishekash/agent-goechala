# Agenturm identity

**A clear view of every agent.**

The logo is a geometric watchtower: three battlements, a single observation
window, and a solid amber silhouette. It represents a clear vantage point over
agent activity, not control of the agents. The wordmark is simply **Agenturm**.

## Assets

| Asset | Use |
| --- | --- |
| [Tower mark](../media/brand/icon.svg) | App icon, avatar, square placements |
| [Transparent tower](../media/brand/icon-transparent.svg) | Dark backgrounds and overlays |
| [Wordmark](../media/brand/logo.svg) | Dark headers and presentations |
| [Hero](../media/brand/hero.svg) | Presentations and wide placements |
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
| Lavender | `#c5adff` | Detail headings and tool inventory |
| Mint | `#86e3b5` | Successful calls and healthy activity |
| Ice | `#86d9ed` | Usage values |
| Frost | `#f6f0e8` | Primary text |
| Slate | `#b9afc5` | Supporting text |
| Rose | `#ff8799` | Failures, accompanied by an explicit status label |

Use color to support text, never as the sole status indicator. Focus remains
visible through borders and selected rows. The identity is static: no flashing
logo or decorative loading animation. The terminal uses the standard monochrome
Unicode tower `♜` followed by **Agenturm**, always on one line. No multi-line
ASCII art, image protocols, emoji rendering, or Nerd Font is required.

## Terminal themes

The command palette exposes exactly four themes: **Current** (Alpine Ember,
the default), **Bright** (light), **Dark** (cool charcoal), and **Solarized**
(blue-green dark). Open **Ctrl+P → Theme** to switch. Panels, table rows,
controls, evidence text, and syntax backgrounds follow the selected palette.

## Naming and compatibility

- Product: **Agenturm**
- Repository, Python distribution, and primary command: **agenturm**
- Compatibility commands: **agent-goechala**, **agent-observatory**
- JavaScript client: **@agenturm/client**
- Former names: Agent Observatory, Agent Goechala

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
The tower artwork is original. Amber marks the active view; supporting labels
remain neutral, and status always has a text label as well as color.

Primary text, supporting text, status accents, and active-tab text are checked
against their backgrounds for at least 4.5:1 contrast. Inactive decorative borders
are intentionally quieter; focused borders use the bright amber accent.

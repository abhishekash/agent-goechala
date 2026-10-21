"""Agenturm's Alpine Ember palette and tower mark."""
from rich.text import Text

NAME = "Agenturm"
COMMAND = "agenturm"
TAGLINE = "A clear view of every agent."
AMBER = "#ffbd69"
LAVENDER = "#c5adff"
MINT = "#86e3b5"
ICE = "#86d9ed"
FROST = "#f6f0e8"
MUTED = "#b9afc5"
# Public aliases retained for capture scripts that imported the first palette.
CYAN, GOLD = ICE, AMBER


def tower_mark(compact=False):
    """A Berlin-style broadcast tower in standard terminal characters."""
    if compact:
        return Text('─●─', style=LAVENDER)
    text = Text('  ╿  \n', style=AMBER)
    text.append(' ▗', style=LAVENDER)
    text.append('⌕', style=f'#121018 on {LAVENDER}')
    text.append('▖ \n ▝▀▘ \n', style=LAVENDER)
    text.append('  ┃  \n ━┻━ ', style=AMBER)
    return text


def masthead(demo=False, compact=False):
    """Keep the wordmark on one line; the tower has its own bounded widget."""
    text = Text("Agenturm", style=f"bold {FROST}")
    if not compact:
        text.append("  /  AGENT MONITOR", style=f"not bold {MUTED}")
    if demo:
        text.append("  /  DEMO", style=AMBER)
    return text

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


def masthead(demo=False, compact=False):
    """A single-line tower and name, visible even in a compact terminal.

    The monochrome rook is a standard Unicode tower, not an emoji or a
    multi-line drawing that can push the product name outside the header.
    """
    text = Text.assemble(
        ("♜", f"bold {AMBER}"), ("  Agenturm", f"bold {FROST}"))
    if not compact:
        text.append("  /  Agent monitor", style=MUTED)
    if demo:
        text.append("  /  DEMO", style=AMBER)
    return text

"""Agent Goechala's Alpine Ember palette and terminal pass mark."""
from rich.text import Text

NAME = "Agent Goechala"
COMMAND = "agent-goechala"
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
    """A two-line block mark: warm and cool ridges meeting at an open pass.

    Compact layouts get a one-line mark. Standard Unicode shapes avoid an
    emoji or Nerd Font dependency.
    """
    if compact:
        text = Text.assemble(
            ("◢◣", f"bold {AMBER}"), ("_", FROST),
            ("◢◣  ", f"bold {LAVENDER}"), ("GOECHALA", f"bold {FROST}"))
        if demo:
            text.append("  /  DEMO", style=AMBER)
        return text
    text = Text.assemble(
        ("  ◢◣", f"bold {AMBER}"), ("   ◢◣   ", f"bold {LAVENDER}"),
        ("GOECHALA", f"bold {FROST}"), ("  /  AGENT MONITOR", MUTED))
    if demo:
        text.append("  /  DEMO", style=AMBER)
    text.append("\n")
    text.append(" ◢██◣", style=AMBER)
    text.append("╱", style=FROST)
    text.append("◢██◣", style=LAVENDER)
    text.append("  A clear view of every agent.", style=MUTED)
    return text

"""Public identity shared by the terminal and demo captures."""
from rich.text import Text

NAME = "Agent Goechala"
COMMAND = "agent-goechala"
TAGLINE = "A clear view of every agent."
CYAN = "#73dce8"
GOLD = "#ffd08a"
FROST = "#f2f7fa"
MUTED = "#9aafc3"

def masthead(demo=False):
    """Two ridges and an open pass, without emoji or an image protocol."""
    text = Text.assemble(
        ("╱╲", f"bold {CYAN}"), ("_", f"bold {GOLD}"),
        ("╱╲  ", f"bold {CYAN}"), (NAME.upper(), f"bold {FROST}"))
    if demo:
        text.append("  /  DEMO", style=MUTED)
    return text

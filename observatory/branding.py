"""Agenturm's Alpine Ember palette and terminal wordmark."""
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
LABEL = "#e1d9eb"
# Public aliases retained for capture scripts that imported the first palette.
CYAN, GOLD = ICE, AMBER

SHORTCUTS = (('/', 'search'), ('e', 'expand'), ('t', 'trace'), ('c', 'copy'), ('q', 'quit'))
# Keep the complete keyboard map in the persistent top-of-screen legend.
SCOPE_HINTS = (
    ('1–7', 'views'), ('/', 'search'), ('e', 'expand'), ('t', 'trace'),
    ('c', 'copy'), ('p', 'pause'), ('d', 'diagnostics'), ('x', 'export'),
    ('q', 'quit'), ('s', 'session activity'), ('a', 'reset'), ('v', 'reviews'),
    ('[', 'newer'), (']', 'older'), ('l', 'latest'), ('alt+←', 'back'),
    ('esc', 'close'), ('ctrl+p', 'palette'),
)


def shortcut_hints(keys=SHORTCUTS):
    """Keyboard hints, styled like the footer keys."""
    text = Text()
    for index, (key, label) in enumerate(keys):
        if index:
            text.append('  ')
        text.append(key, style=f'bold #121018 on {AMBER}')
        text.append(f' {label}', style=LABEL)
    return text


def masthead(demo=False, compact=False):
    """The wordmark, set as text: a name, a divider, and the tagline."""
    text = Text(NAME, style=f"bold {FROST}")
    if not compact:
        text.append("  │  ", style=MUTED)
        text.append(TAGLINE, style=MUTED)
    if demo:
        text.append("   ", style=MUTED)
        text.append(" DEMO ", style=f"bold #121018 on {AMBER}")
    return text

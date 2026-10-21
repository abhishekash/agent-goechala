"""Four supported terminal palettes; Current preserves the Alpine Ember identity."""
from rich.text import Span
from textual.theme import Theme


# Semantic roles shared by TCSS and Rich-rendered evidence, not just palette chrome.
CURRENT = {
    'canvas': '#121018', 'surface': '#191621', 'panel': '#242030',
    'text': '#f6f0e8', 'label': '#e1d9eb', 'muted': '#b9afc5', 'disabled': '#8f849d',
    'primary': '#ffbd69', 'secondary': '#c5adff', 'success': '#86e3b5',
    'info': '#86d9ed', 'error': '#ff8799',
    'border': '#51475f', 'border-subtle': '#463d52', 'border-disabled': '#393041',
    'scrollbar': '#675975', 'hover': '#3a2c49', 'focus': '#493622',
    'on-primary': '#21182a', 'primary-focus': '#ffdb9f', 'error-surface': '#422330',
    'selection': '#5c4578', 'on-selection': '#ffffff', 'cursor': '#443555',
    'on-cursor': '#fff7eb', 'header': '#30283e', 'row-hover': '#352b43',
    'row-alt': '#211b2b', 'footer-key': '#3b2d4c',
}


def palette(**overrides):
    return {**CURRENT, **{key.replace('_', '-'): value for key, value in overrides.items()}}


PALETTES = {
    'Current': CURRENT,
    'Bright': palette(
        canvas='#ffffff', surface='#f5f6fa', panel='#e8ebf2',
        text='#18202f', label='#253047', muted='#4d5870', disabled='#606b80',
        primary='#805000', secondary='#594095', success='#206540', info='#075b79', error='#ac2444',
        border='#8a94a8', border_subtle='#a9b1c1', border_disabled='#c6cbd6', scrollbar='#8692aa',
        hover='#dcdff0', focus='#fff0cc', on_primary='#ffffff', primary_focus='#674000',
        error_surface='#fbe3e9', selection='#c8d6f3', on_selection='#18202f',
        cursor='#d4ddf0', on_cursor='#18202f', header='#e0e5f0', row_hover='#e1e6f4',
        row_alt='#eceff6', footer_key='#dde2ee',
    ),
    'Dark': palette(
        canvas='#090c10', surface='#10151c', panel='#1c2430',
        text='#edf2f7', label='#d4deea', muted='#aab8ca', disabled='#8291a6',
        primary='#8ecbff', secondary='#c5b0ff', success='#7de0a6', info='#81dbe8', error='#ff91a3',
        border='#48576c', border_subtle='#354256', border_disabled='#2b3544', scrollbar='#60718a',
        hover='#28374a', focus='#243c54', on_primary='#090c10', primary_focus='#b5dfff',
        error_surface='#40232d', selection='#34516f', on_selection='#ffffff',
        cursor='#2c4663', on_cursor='#ffffff', header='#202e40', row_hover='#243247',
        row_alt='#151e2a', footer_key='#26354b',
    ),
    'Solarized': palette(
        canvas='#002b36', surface='#073642', panel='#0b404d',
        text='#fdf6e3', label='#eee8d5', muted='#93a1a1', disabled='#839496',
        primary='#e1b900', secondary='#a6a4e8', success='#b3c34c', info='#56c3d1', error='#ff806e',
        border='#527b83', border_subtle='#3b626c', border_disabled='#2e505b', scrollbar='#657b83',
        hover='#124c59', focus='#40513b', on_primary='#002b36', primary_focus='#f0cf55',
        error_surface='#49383b', selection='#285967', on_selection='#fdf6e3',
        cursor='#285967', on_cursor='#fdf6e3', header='#164854', row_hover='#124451',
        row_alt='#0a3c48', footer_key='#1b4b58',
    ),
}


def supported_themes():
    return [Theme(
        name=name, primary=colors['primary'], secondary=colors['secondary'],
        accent=colors['info'], warning=colors['primary'], error=colors['error'],
        success=colors['success'], foreground=colors['text'], background=colors['canvas'],
        surface=colors['surface'], panel=colors['panel'], dark=name != 'Bright',
        variables={f'agent-{key}': value for key, value in colors.items()},
    ) for name, colors in PALETTES.items()]


def themed_style(widget, style):
    """Translate the original Rich styles, including cached table/evidence colors."""
    colors = PALETTES.get(widget.app.theme, CURRENT)
    for role, original in CURRENT.items():
        style = style.replace(original, colors[role])
    return style


def themed_text(widget, text):
    text = text.copy()
    text.style = themed_style(widget, str(text.style))
    text.spans = [Span(span.start, span.end, themed_style(widget, str(span.style)))
                  for span in text.spans]
    return text

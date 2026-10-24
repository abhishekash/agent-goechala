"""Avoid invalidating unchanged terminal status widgets on each timer tick."""


def update_status_text(widget, text):
    # Text.__eq__ does not include its base style, which matters across themes.
    signature = (text.plain, str(text.style), tuple(text.spans))
    if getattr(widget, '_agenturm_status_signature', None) == signature:
        return False
    widget.update(text)
    widget._agenturm_status_signature = signature
    return True

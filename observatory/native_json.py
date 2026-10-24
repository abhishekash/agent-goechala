"""Depth limits for bounded, untrusted native JSON records."""
import json

MAX_DEPTH = 64


def loads(raw):
    try:
        value = json.loads(raw)
    except RecursionError:
        raise ValueError("native JSON nesting exceeds limit") from None
    # Use iterators rather than copying wide containers or recursively walking
    # attacker-controlled structures. Strings (including encoded tool output)
    # are leaves; consumers parsing those strings use this same helper.
    stack = []
    if isinstance(value, (dict, list)):
        stack.append(iter(value.values() if isinstance(value, dict) else value))
    while stack:
        child = next(stack[-1], _END)
        if child is _END:
            stack.pop()
        elif isinstance(child, (dict, list)):
            if len(stack) >= MAX_DEPTH:
                raise ValueError("native JSON nesting exceeds limit")
            stack.append(iter(child.values() if isinstance(child, dict) else child))
    return value


_END = object()

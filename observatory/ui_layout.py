"""Column budgeting independent of Textual's layout lifecycle."""

# Lowest-value columns disappear first; full records remain in the inspector.
DROP_ORDER = {
    'agent-table': (1, 4, 2, 0),
    'activity-table': (1, 0),
    'mcp-servers': (4, 3, 1, 2),
    'mcp-table': (1, 5, 0, 2),
    'trace-table': (3, 2),
    'host-table': (5, 1, 4, 2),
    'metrics-table': (5, 2, 4, 1),
    'finding-table': (4, 3, 1),
}


def column_layout(table_id, specs, width):
    """Return original column indices and widths within the available cells."""
    visible = list(range(len(specs)))
    for index in DROP_ORDER.get(table_id, ()):
        required = sum(specs[i][1] + 2 for i in visible) + 1
        if required <= width:
            break
        if index in visible:
            visible.remove(index)
    available = max(len(visible), width - 2 * len(visible) - 1)
    widths = [specs[i][1] for i in visible]
    # Very small terminals still get useful subject/result columns, not a
    # horizontal scrollbar hiding the subject behind metadata.
    while sum(widths) > available and max(widths) > 1:
        widest = max(range(len(widths)), key=widths.__getitem__)
        widths[widest] -= 1
    spare = max(0, available - sum(widths))
    weight = sum(specs[i][2] for i in visible) or 1
    for position, index in enumerate(visible):
        widths[position] += int(spare * specs[index][2] / weight)
    if visible:
        flexible = max(range(len(visible)), key=lambda p: specs[visible[p]][2])
        widths[flexible] += max(0, available - sum(widths))
    return tuple(visible), tuple(widths)

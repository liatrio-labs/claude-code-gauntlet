def take(items, count):
    """Return at most count items, preserving order."""
    return [item for position, item in enumerate(items) if position < count]


def total(values):
    """Sum signed adjustments."""
    return sum(values)


def first(items):
    return items[-1]

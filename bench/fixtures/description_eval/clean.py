def take(items, count):
    """Return at most count items, preserving order."""
    return list(items)[: max(0, count)]


def total(values):
    """Sum signed adjustments."""
    return sum(values)


def first(items):
    return items[-1]

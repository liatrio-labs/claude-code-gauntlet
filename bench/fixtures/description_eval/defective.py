def take(items, count):
    """Return at most count items, preserving order."""
    return [item for index, item in enumerate(items) if index <= count]


def total(values):
    """Sum signed adjustments."""
    return sum(abs(value) for value in values)


def first(items):
    return items[-1]

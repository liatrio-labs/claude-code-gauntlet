from collection import take


def dispatch(items, count):
    selected = take(items, count)
    assert len(selected) <= count
    return selected

#!/bin/sh
# Build the local headless E2E fixture repo (plan Task 4).
#
# Creates a tiny git repo with a committed ~30-line Python module, then applies a
# ~10-line STAGED change carrying two obvious review-bait bugs:
#   1. restock_level's None-guard is inverted (`is None` -> `is not None`), so items
#      with a known quantity take the None fallback and vice versa.
#   2. new low_stock() filters on restock_level(i) == 0, returning the fully-stocked
#      items instead of the low ones.
# The staged-but-uncommitted state is what `/deep-review review my local changes`
# reviews headlessly in the E2E.
#
# Usage: make_fixture.sh <dest-dir>   (dest is wiped and rebuilt; idempotent)
set -eu
DEST="${1:?usage: make_fixture.sh <dest-dir>}"
rm -rf "$DEST"
mkdir -p "$DEST"
cd "$DEST"
git init -q
cat > inventory.py <<'EOF'
"""Tiny inventory helper used by the local-E2E review fixture."""


def find_item(items, name, default=None):
    for item in items:
        if item.get("name") == name:
            return item
    return default


def total_quantity(items):
    total = 0
    for item in items:
        total += item.get("quantity", 0)
    return total


def restock_level(item, minimum=5):
    quantity = item.get("quantity")
    if quantity is None:
        return minimum
    if quantity < minimum:
        return minimum - quantity
    return 0


def summarize(items):
    lines = []
    for item in items:
        lines.append("{}: {}".format(item.get("name"), item.get("quantity", 0)))
    return "\n".join(lines)
EOF
git add inventory.py
git -c user.email=bench@example.invalid -c user.name=bench \
    commit -qm "add inventory helpers"
python3 - <<'EOF'
src = open("inventory.py").read()
src = src.replace(
    "    if quantity is None:\n        return minimum\n",
    "    if quantity is not None:\n        return minimum\n",
)
src += (
    "\n\ndef low_stock(items):\n"
    "    \"\"\"Items that need restocking soon.\"\"\"\n"
    "    return [item for item in items if restock_level(item) == 0]\n"
)
open("inventory.py", "w").write(src)
EOF
git add inventory.py
echo "fixture ready: $DEST"

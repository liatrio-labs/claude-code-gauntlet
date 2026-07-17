"""PR-granular checkpointing for bench runs (spec H3).

A run's per-PR status lives in one JSON file per golden URL under
``{run_dir}/state/``. The on-disk files are the source of truth, so a
killed pass loses at most the single PR that was mid-flight and a fresh
process can resume by reading state back.

Resume semantics
-----------------
Valid statuses: ``pending | ok | timeout | invalid | drifted | failed``.

* Plain ``--resume`` re-runs only URLs still in ``pending`` — ``ok``,
  ``invalid`` and ``drifted`` are terminal, and ``timeout``/``failed`` are
  deliberately *not* retried without an explicit opt-in.
* ``--retry-failed`` re-runs exactly the ``timeout`` and ``failed`` set.

``pending(urls)`` and ``failed(urls)`` expose those two sets; both preserve
the caller's input order.
"""

import hashlib
import json
import os
import re
import time

VALID_STATUSES = ("pending", "ok", "timeout", "invalid", "drifted", "failed")

# Statuses that a plain resume treats as "done" and skips. Everything else
# (i.e. only "pending") still needs a run.
_RESUME_SKIP = frozenset({"ok", "invalid", "drifted", "timeout", "failed"})

# Statuses that --retry-failed re-runs.
_FAILED = frozenset({"timeout", "failed"})


def _pr_filename(url):
    """Map a golden URL to a deterministic, filesystem-safe filename.

    A slug keeps the file human-recognizable; a hash suffix guarantees
    uniqueness even when two URLs slugify identically or the slug is
    truncated.
    """
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    slug = re.sub(r"[^A-Za-z0-9]+", "-", url).strip("-").lower()[:80]
    return f"{slug}-{digest}.json" if slug else f"{digest}.json"


class Checkpoint:
    def __init__(self, run_dir):
        self.run_dir = str(run_dir)
        self.state_dir = os.path.join(self.run_dir, "state")
        os.makedirs(self.state_dir, exist_ok=True)

    def _path(self, url):
        return os.path.join(self.state_dir, _pr_filename(url))

    def status(self, url):
        """Return the recorded status for ``url``, or ``pending`` if unseen."""
        path = self._path(url)
        if not os.path.exists(path):
            return "pending"
        with open(path) as fh:
            return json.load(fh)["status"]

    def mark(self, url, status, detail=None):
        """Persist ``status`` (and optional ``detail``) for ``url``.

        Raises ``ValueError`` for a status outside ``VALID_STATUSES``; no file
        is written in that case.
        """
        if status not in VALID_STATUSES:
            raise ValueError(
                f"invalid checkpoint status {status!r}; "
                f"expected one of {VALID_STATUSES}"
            )
        record = {
            "url": url,
            "status": status,
            "detail": detail,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        tmp = self._path(url) + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(record, fh)
        os.replace(tmp, self._path(url))

    def pending(self, urls):
        """URLs a plain resume must still run (status not in the skip set)."""
        return [u for u in urls if self.status(u) not in _RESUME_SKIP]

    def failed(self, urls):
        """URLs that ``--retry-failed`` re-runs (timeout or failed)."""
        return [u for u in urls if self.status(u) in _FAILED]

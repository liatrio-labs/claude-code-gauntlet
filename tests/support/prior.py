"""Input snapshots for delivery retries through the real prior-state reader."""

from gauntlet.forge import JsonFetch
from gauntlet.marker import build_finding_marker, build_footer
from gauntlet.prior_review import PriorDelivery


def prior_notes(state: PriorDelivery | None, sha: str) -> JsonFetch:
    if state is None:
        return JsonFetch([], None)
    notes: list[dict[str, str]] = []
    if state.summary_posted:
        notes.append({"body": build_footer(0, sha)})
    for key in sorted(state.finding_keys):
        body = build_finding_marker(sha, key)
        if key in state.legacy_group_keys:
            body = "Corroborating finding \u2014 member\n" + body
        notes.append({"body": body})
    return JsonFetch(notes, state.error)

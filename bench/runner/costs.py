"""Cost/token parsing for the ``claude -p --output-format json`` result envelope.

The envelope (probe artifact 33) exposes ``total_cost_usd`` (float), an aggregate
``usage`` object of token classes, and ``modelUsage`` keyed by model id -> per-model
usage. ``parse_costs`` collapses those into the numbers the ledger records, summing
every top-level token class it finds (input/output/cache) without hardcoding exact
key spellings so it survives the CLI's camelCase (``inputTokens``) vs the aggregate's
snake_case (``input_tokens``). Nested objects (e.g. ``cache_creation``) are skipped so
their components are never double-counted.

A run may report MORE than one model in ``modelUsage`` (e.g. a primary model plus a
background bookkeeping call), and model ids are opaque and may carry variant suffixes
(observed: ``claude-opus-4-8[1m]``). ``per_model`` therefore passes every key through
verbatim, and ``tokens_total`` sums across all of them -- via the aggregate ``.usage``
when present, else by summing every ``modelUsage`` entry. No model id is ever assumed.
"""

__all__ = ["parse_costs"]


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _sum_tokens(usage):
    """Sum every top-level integer USAGE field whose name mentions "token".

    Capacity/limit fields (``maxOutputTokens``, anything max/limit-prefixed) sit
    beside the real counters in ``modelUsage`` entries and are model properties,
    not consumption — they are excluded.
    """
    if not isinstance(usage, dict):
        return 0
    total = 0
    for key, value in usage.items():
        # bool is an int subclass; exclude it. Only top-level ints are summed so a
        # nested breakdown (dict) never double-counts its parent aggregate.
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        lowered = key.lower()
        if "token" not in lowered:
            continue
        if lowered.startswith("max") or "limit" in lowered:
            continue
        total += value
    return total


def parse_costs(envelope):
    """Return ``{"cost_usd", "tokens_total", "per_model"}`` from a result envelope."""
    if not isinstance(envelope, dict):
        envelope = {}

    cost_usd = _to_float(envelope.get("total_cost_usd"))

    per_model = {}
    model_usage = envelope.get("modelUsage") or {}
    if isinstance(model_usage, dict):
        for model, usage in model_usage.items():
            if not isinstance(usage, dict):
                continue
            cost = usage.get("costUSD")
            if cost is None:
                cost = usage.get("cost_usd")
            per_model[model] = {
                "tokens": _sum_tokens(usage),
                "cost_usd": _to_float(cost),
            }

    # Prefer the authoritative run aggregate; if it is missing, sum across every model.
    aggregate = envelope.get("usage")
    if isinstance(aggregate, dict) and aggregate:
        tokens_total = _sum_tokens(aggregate)
    else:
        tokens_total = sum(pm["tokens"] for pm in per_model.values())

    return {"cost_usd": cost_usd, "tokens_total": tokens_total, "per_model": per_model}

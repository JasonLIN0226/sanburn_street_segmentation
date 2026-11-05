"""Estimate model usage from recorded Responses API usage data."""

from pathlib import Path

from storage import read_json

MODEL = "gpt-6.1-sol"
PRICE = {"input": 2.0, "cached": 0.10, "write_30m": 2.50, "write_12h": 5.0, "output": 10.0}


def cost_estimate(rows, case):
    total_in = total_out = cached = writes = 0
    cost = 0.0
    complete = True
    notes = set()
    if not rows:
        return {
            "tokens": None,
            "input": None,
            "output": None,
            "cached": None,
            "cost_usd": None,
            "estimate_notes": ["No API usage recorded"],
        }
    for row in rows:
        u = row.get("usage")
        if (
            not isinstance(u, dict)
            or not isinstance(u.get("input_tokens"), int)
            or not isinstance(u.get("output_tokens"), int)
        ):
            complete = False
            continue
        inp = u["input_tokens"]
        out = u["output_tokens"]
        d = u.get("input_tokens_details") or {}
        read = d.get("cached_tokens", 0)
        write = d.get("cache_write_tokens", 0)
        if (
            not all(isinstance(x, int) and x >= 0 for x in (inp, out, read, write))
            or read + write > inp
        ):
            complete = False
            continue
        total_in += inp
        total_out += out
        cached += read
        writes += write
        if row.get("model") != MODEL:
            complete = False
            notes.add("Unknown model or pricing")
            continue
        response = read_json(Path(case) / "responses" / f"{row.get('seq',0):04d}.json") or {}
        tier = response.get("service_tier", "default")
        if tier not in ("default", None):
            complete = False
            notes.add("Nonstandard or uncertain service tier excluded")
            continue
        details = d.get("cache_write_tokens_details") or {}
        if details:
            w30 = details.get("ttl_30m_tokens", 0)
            w12 = details.get("ttl_12h_tokens", 0)
            if not isinstance(w30, int) or not isinstance(w12, int) or w30 + w12 != write:
                complete = False
                notes.add("Incomplete cache-write breakdown")
                continue
        else:
            w30 = write
            w12 = 0
            if write:
                notes.add("Cache-write duration unavailable; 30 minutes assumed")
        im = 2 if inp > 272000 else 1
        om = 1.5 if inp > 272000 else 1
        cost += (
            (inp - read - write) * PRICE["input"] * im
            + read * PRICE["cached"] * im
            + w30 * PRICE["write_30m"] * im
            + w12 * PRICE["write_12h"] * im
            + out * PRICE["output"] * om
        ) / 1_000_000
    if not complete:
        notes.add("Some usage or pricing information is missing")
    return {
        "tokens": total_in + total_out,
        "input": total_in,
        "output": total_out,
        "cached": cached,
        "writes": writes,
        "cost_usd": round(cost, 6) if complete else None,
        "estimate_notes": sorted(notes),
        "request_count": len(rows),
    }

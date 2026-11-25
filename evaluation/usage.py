"""Summarize estimated USD and provider-reported tokens for an evaluation report."""
import argparse
import json
from collections import Counter
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path


PRICE_TABLE = Path(__file__).with_name("token_prices.json")
LEGACY_PRICE_CARD = "codex-standard-2026-11-25"


def price_card(version=None):
    cards = json.loads(PRICE_TABLE.read_text())
    version = version or max(cards)
    return {"version": version, **cards[version]}


def estimate_cost(row, manifest):
    usage = row.get("token_usage", {})
    card = manifest.get("price_card")
    if not usage or not card:
        return {}
    models = {"bank": manifest.get("model"),
              "customer": manifest.get("customer", {}).get("model", manifest.get("model"))}
    amounts = {}
    for actor, model in models.items():
        counts = usage.get(actor, {})
        if counts.get("calls") == counts.get("reported_calls") == 0 and counts.get("total_tokens") == 0:
            amounts[actor] = Decimal(0)
            continue
        rates = card["usd_per_million_tokens"].get(model)
        if not rates or "total_tokens" not in counts or counts.get("calls") != counts.get("reported_calls"):
            continue
        cached = counts.get("cached_input_tokens")
        if cached is None or not 0 <= cached <= counts["input_tokens"]:
            continue
        amounts[actor] = ((counts["input_tokens"] - cached) * Decimal(rates["input"])
                          + cached * Decimal(rates["cached_input"])
                          + counts["output_tokens"] * Decimal(rates["output"])) / 1_000_000
    if len(amounts) == len(models):
        amounts["total"] = sum(amounts.values())
    return {"price_card": card["version"], **{k: float(v) for k, v in amounts.items()}}


def report_cost(row, manifest):
    if "estimated_cost_usd" in row:
        return row["estimated_cost_usd"]
    if row.get("token_usage") and "price_card" not in manifest:
        manifest = {**manifest, "price_card": price_card(LEGACY_PRICE_CARD)}
    return estimate_cost(row, manifest)


def actor_usage(paths):
    calls = 0
    reports = []
    for path in paths:
        with Path(path).open() as source:
            for event in map(json.loads, source):
                kind = event["event_type"]
                if kind in ("model.requested", "context.summary_requested"):
                    calls += 1
                if kind in ("model.responded", "model.failed", "generation.failed",
                            "context.summary_completed", "context.summary_failed"):
                    usage = event["payload"].get("usage")
                    if usage is not None:
                        reports.append(usage)
    counts = {"calls": calls, "reported_calls": len(reports)}
    if reports:
        counts["reported_tokens"] = sum(r["total_tokens"] for r in reports)
    if calls == len(reports):
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            counts[key] = sum(r[key] for r in reports)
        for key in ("cached_input_tokens", "reasoning_output_tokens"):
            if reports and all(key in r for r in reports):
                counts[key] = sum(r[key] for r in reports)
    return counts


def usage_scorecard(outcome, customer):
    customer_paths = customer.audit_paths if customer else []
    usage = {"customer": actor_usage(customer_paths)}
    if outcome.turn_audits or not outcome.error:
        usage["bank"] = actor_usage(outcome.turn_audits)
    return {**asdict(outcome), "customer_audits": customer_paths, "token_usage": usage}


def summarize(rows, manifest=None):
    statuses = Counter(r["status"] for r in rows)
    costs = [report_cost(r, manifest or {}) for r in rows]
    spend = {"fully_priced_attempts": sum("total" in c for c in costs), "total": None}
    for key in ("bank", "customer", "total"):
        if costs and all(key in c for c in costs):
            spend[key] = float(sum(Decimal(str(c[key])) for c in costs))
    result = {"estimated_cost_usd": spend, "attempts": len(rows), "statuses": dict(statuses)}
    operations = [r.get("dimensions", {}).get("operational") for r in rows if r["status"] != "ERROR"]
    for actor in ("bank", "customer"):
        records = [row.get("token_usage", {}).get(actor, {}) for row in rows]
        complete = [r["total_tokens"] for r in records if "total_tokens" in r]
        totals = {"fully_measured_attempts": len(complete),
                  "reported_calls": sum(r.get("reported_calls", 0) for r in records),
                  "observed_calls": sum(r.get("calls", 0) for r in records)}
        if complete and len(complete) == len(rows):
            totals["total_tokens"] = sum(complete)
            if operations.count("PASS") and all(o in ("PASS", "FAIL") for o in operations):
                totals["tokens_per_operational_pass"] = sum(complete) / operations.count("PASS")
            if statuses["PASS"] and not statuses["REVIEW"]:
                totals["tokens_per_verified_resolution"] = sum(complete) / statuses["PASS"]
                if actor in spend:
                    totals["estimated_usd_per_verified_resolution"] = spend[actor] / statuses["PASS"]
        result[actor] = totals
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    report = parser.parse_args().report
    manifest = report.with_name("manifest.json")
    print(json.dumps(summarize(json.loads(report.read_text()),
                               json.loads(manifest.read_text()) if manifest.exists() else {}), indent=2))

"""Per-call usage records; estimates are never presented as billed charges."""
from datetime import datetime, timezone
from time import perf_counter
import re

from .storage import save_json

PRICING_DATE = "2026-09-05"
PRICING_SOURCE = "https://developers.openai.com/api/docs/pricing"


def estimate_cost(model, usage, service_tier):
    model = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", model)
    base = {"currency": "USD", "amount": None, "label": "estimated_cost",
            "pricing_date": PRICING_DATE, "pricing_source": PRICING_SOURCE}
    if not usage:
        return dict(base, reason="Usage unavailable; unknown does not mean free.")
    if model == "gpt-transcribe" and usage.get("type") == "duration":
        seconds = usage.get("seconds")
        if seconds is not None:
            return dict(base, amount=round(seconds / 60 * 0.0045, 9),
                        calculation={"audio_seconds": seconds, "usd_per_minute": 0.0045})
    if model not in {"gpt-5.6-sol", "gpt-5.6", "gpt-4.1"}:
        return dict(base, reason="No verified pricing configured for this model.")
    if service_tier not in {None, "default", "auto"}:
        return dict(base, reason="No pricing configured for the returned service tier.")
    incoming, outgoing = usage.get("input_tokens"), usage.get("output_tokens")
    if incoming is None or outgoing is None:
        return dict(base, reason="Token counts unavailable.")
    details = usage.get("input_tokens_details") or {}
    cached, writes = details.get("cached_tokens", 0), details.get("cache_write_tokens", 0)
    ordinary = incoming - cached - writes
    if min(ordinary, cached, writes, outgoing) < 0:
        return dict(base, reason="Inconsistent token usage.")
    rates = [4, 0.4, 5, 20] if model != "gpt-4.1" else [2, 0.5, 2.5, 8]
    if model != "gpt-4.1" and incoming > 272000:
        rates = [8, 0.8, 10, 30]
    counts = [ordinary, cached, writes, outgoing]
    return dict(base, amount=round(sum(n * r for n, r in zip(counts, rates)) / 1_000_000, 9),
                calculation={"ordinary_input": ordinary, "cached_input": cached,
                             "cache_write_input": writes, "output_including_reasoning": outgoing,
                             "rates_usd_per_million_in_same_order": rates},
                assumptions="Standard direct API pricing; excludes taxes, credits and account adjustments.")


class CallLog:
    def __init__(self, run_dir):
        self.path = run_dir / "calls.json"
        self.calls = []
        self.started = perf_counter()

    def call(self, stage, model, invoke):
        record = {"stage": stage, "requested_model": model, "returned_model": None,
                  "service_tier": None, "request_id": None, "response_id": None,
                  "started_at": datetime.now(timezone.utc).isoformat(), "status": "started",
                  "usage": None, "elapsed_seconds": None, "estimated_cost": None}
        self.calls.append(record)
        self.save()
        started = perf_counter()
        try:
            result = invoke()
            usage = getattr(result, "usage", None)
            reasoning = getattr(result, "reasoning", None)
            record["reasoning_effort"] = getattr(reasoning, "effort", None)
            record.update(status="returned", returned_model=getattr(result, "model", None),
                          service_tier=getattr(result, "service_tier", None),
                          request_id=getattr(result, "_request_id", None),
                          response_id=getattr(result, "id", None),
                          usage=usage.model_dump(mode="json") if hasattr(usage, "model_dump") else usage)
            return result
        except Exception as exc:
            # Avoid logging exception messages, headers, keys, or consultation contents.
            record.update(status="failed", error_type=type(exc).__name__,
                          request_id=getattr(exc, "request_id", None))
            raise
        finally:
            record["elapsed_seconds"] = round(perf_counter() - started, 3)
            record["finished_at"] = datetime.now(timezone.utc).isoformat()
            record["estimated_cost"] = estimate_cost(
                record["returned_model"] or model, record["usage"], record["service_tier"])
            self.save()

    def save(self):
        known = [c["estimated_cost"]["amount"] for c in self.calls
                 if c.get("estimated_cost") and c["estimated_cost"]["amount"] is not None]
        complete = bool(self.calls) and len(known) == len(self.calls)
        summary = {"calls": self.calls, "elapsed_seconds": round(perf_counter() - self.started, 3),
                             "estimated_cost_usd": round(sum(known), 9) if complete else None,
                             "known_estimated_subtotal_usd": round(sum(known), 9),
                             "estimate_complete": complete, "reported_cost_usd": None,
                             "reconciliation_status": "not_reconciled"}
        save_json(self.path, summary)
        return summary

    def finish(self):
        summary = self.save()
        return {"elapsed_seconds": summary["elapsed_seconds"],
                "estimated_cost_usd": summary["estimated_cost_usd"],
                "estimate_complete": summary["estimate_complete"],
                "reported_cost_usd": None, "reconciliation_status": "not_reconciled",
                "calls": [{"stage": c["stage"], "model": c["returned_model"] or c["requested_model"],
                           "elapsed_seconds": c["elapsed_seconds"],
                           "estimated_cost_usd": (c.get("estimated_cost") or {}).get("amount")}
                          for c in self.calls]}

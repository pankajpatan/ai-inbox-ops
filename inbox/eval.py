"""Measure a classifier against labelled emails.

    python -m inbox.eval                                   # keyword rules, free
    python -m inbox.eval --classifier claude               # uses ANTHROPIC_API_KEY
    python -m inbox.eval --classifier claude --model claude-haiku-5-5 --save

Each case in evals/emails.jsonl has the expected category, urgency and needs_reply, and
optionally fields (phone, invoice_number, amount) that must be extracted. The answers are
scored after the same code checks the app runs (checks.check), so this measures what the
business would actually see.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv

from .checks import check, digits, norm
from .classifier import KeywordClassifier, cost_usd
from .models import Email
from .rules import Rules, load_rules

ROOT = Path(__file__).resolve().parent.parent
RANK = {"later": 0, "today": 1, "now": 2}


def load_cases(path: Path) -> list[dict]:
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = [c["id"] for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("case ids must be unique")
    return cases


def same_field(name: str, expected: str, got: str | None) -> bool:
    if got is None:
        return False
    if name in ("phone", "amount"):
        return digits(expected) == digits(got)
    return norm(expected) == norm(got)


def run_case(classifier, case: dict, rules: Rules) -> dict:
    e = Email(message_id=case["id"], thread_id=case["id"], from_name=case.get("from_name", ""),
              from_email=f"{case['id']}@example.com", subject=case.get("subject", ""), body=case["body"],
              received_at=time.time())
    started = time.monotonic()
    try:
        raw = classifier.classify(e, rules)
    except Exception as exc:
        return {"id": case["id"], "error": f"{type(exc).__name__}: {exc}"}
    checked = check(raw, e)
    c = checked.classification
    fields = case.get("fields", {})
    extracted = c.extracted.model_dump()
    return {
        "id": case["id"], "note": case.get("note", ""),
        "expected": {"category": case["category"], "urgency": case["urgency"], "needs_reply": case["needs_reply"]},
        "got": {"category": c.category, "urgency": c.urgency, "needs_reply": c.needs_reply},
        "fields_ok": {k: same_field(k, v, extracted.get(k)) for k, v in fields.items()},
        "fields_got": {k: extracted.get(k) for k in fields},
        "corrections": checked.corrections,
        "summary": c.summary, "meta": raw.meta, "seconds": round(time.monotonic() - started, 2),
    }


def report(results: list[dict], label: str, model: str | None) -> dict:
    ok = [r for r in results if "error" not in r]
    n = len(results)
    hits = lambda k: sum(r["got"][k] == r["expected"][k] for r in ok)  # noqa: E731
    emergencies = [r for r in ok if r["expected"]["urgency"] == "now"]
    under = [r for r in ok if RANK[r["got"]["urgency"]] < RANK[r["expected"]["urgency"]]]
    over = [r for r in ok if RANK[r["got"]["urgency"]] > RANK[r["expected"]["urgency"]]]
    field_checks = [v for r in ok for v in r["fields_ok"].values()]
    tin = sum(r["meta"].get("input_tokens", 0) for r in ok)
    tout = sum(r["meta"].get("output_tokens", 0) for r in ok)
    summary = {
        "classifier": label, "cases": n, "errors": n - len(ok),
        "category_correct": f"{hits('category')}/{n}",
        "urgency_correct": f"{hits('urgency')}/{n}",
        "emergencies_caught": f"{sum(r['got']['urgency'] == 'now' for r in emergencies)}/{len(emergencies)}",
        "urgency_under_called": len(under),
        "urgency_over_called": len(over),
        "needs_reply_correct": f"{hits('needs_reply')}/{n}",
        "fields_extracted": f"{sum(field_checks)}/{len(field_checks)}",
        "answers_corrected_by_checks": sum(len(r["corrections"]) for r in ok),
        "input_tokens": tin, "output_tokens": tout,
        "avg_seconds": round(sum(r["seconds"] for r in ok) / max(len(ok), 1), 2),
    }
    cost = cost_usd(model, tin, tout) if model else None
    if cost is not None:
        summary["cost_usd_total"] = round(cost, 4)
        summary["cost_usd_per_email"] = round(cost / max(len(ok), 1), 5)
    width = max(map(len, summary))
    for k, v in summary.items():
        print(f"{k:<{width}}  {v}")
    misses = [r for r in results if "error" in r or r["got"] != r["expected"] or not all(r["fields_ok"].values())]
    if misses:
        print("\nDifferences:")
    for r in misses:
        if "error" in r:
            print(f"- {r['id']}: ERROR {r['error']}")
            continue
        diffs = [f"{k} expected {r['expected'][k]} got {r['got'][k]}" for k in r["expected"]
                 if r["expected"][k] != r["got"][k]]
        diffs += [f"{k} not extracted (got {r['fields_got'][k]!r})" for k, good in r["fields_ok"].items() if not good]
        print(f"- {r['id']} ({r['note']}): " + "; ".join(diffs))
        for c in r["corrections"]:
            print(f"    check: {c}")
    return summary


def main() -> None:
    load_dotenv(ROOT / ".env")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--classifier", choices=["keywords", "claude"], default="keywords")
    p.add_argument("--model", default=os.getenv("INBOX_MODEL", "claude-opus-5-5"))
    p.add_argument("--effort", default=os.getenv("INBOX_EFFORT", "low"))
    p.add_argument("--cases", default=str(ROOT / "evals" / "emails.jsonl"))
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--save", action="store_true")
    args = p.parse_args()

    rules = load_rules(ROOT / "routing.yaml")
    cases = load_cases(Path(args.cases))
    model = None
    if args.classifier == "claude":
        from .classifier import ClaudeClassifier
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise SystemExit("Set ANTHROPIC_API_KEY in .env to run the Claude eval.")
        classifier, model = ClaudeClassifier(os.environ["ANTHROPIC_API_KEY"], args.model, args.effort), args.model
    else:
        classifier = KeywordClassifier()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda c: run_case(classifier, c, rules), cases))
    summary = report(results, classifier.name, model)
    if args.save:
        out = ROOT / "evals" / "results"
        out.mkdir(parents=True, exist_ok=True)
        name = f"{classifier.name.replace(':', '-')}-{args.effort}.json"
        (out / name).write_text(json.dumps({"summary": summary, "results": results}, indent=2, ensure_ascii=False))
        print(f"\nSaved {out / name}")


if __name__ == "__main__":
    main()

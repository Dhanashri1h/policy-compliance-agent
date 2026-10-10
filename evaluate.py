#!/usr/bin/env python3
"""Score agent output against synthetic ground truth: per-rule P/R/F1 + decision accuracy."""
import argparse, json
from collections import Counter

ap = argparse.ArgumentParser()
ap.add_argument("--data", default="data"); ap.add_argument("--policy", default="policy.json")
ap.add_argument("--out", default="outputs"); ap.add_argument("--min-f1", type=float, default=0.0)
a = ap.parse_args()

policy = json.load(open(a.policy)); truth = json.load(open(f"{a.data}/ground_truth.json"))
sev = {r["id"]: r["severity"] for r in policy["rules"]}
preds = {}
for line in open(f"{a.out}/decisions.jsonl"):
    d = json.loads(line); preds[d["claim_id"]] = d


def expected_decision(labels):
    for s in ["high", "medium", "low"]:
        if any(sev[l] == s for l in labels): return policy["decision_map"][s]
    return policy["decision_map"]["none"]


lines = ["# Evaluation report", "", "| Rule | Name | TP | FP | FN | Precision | Recall | F1 |", "|---|---|---|---|---|---|---|---|"]
f1s = []
for r in policy["rules"]:
    tp = fp = fn = 0
    for cid, labels in truth.items():
        got = r["id"] in [v["rule_id"] for v in preds[cid]["violations"]]; exp = r["id"] in labels
        tp += got and exp; fp += got and not exp; fn += exp and not got
    p = tp / (tp + fp) if tp + fp else 1.0; rc = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * p * rc / (p + rc) if p + rc else 0.0; f1s.append(f1)
    lines.append(f"| {r['id']} | {r['name']} | {tp} | {fp} | {fn} | {p:.3f} | {rc:.3f} | {f1:.3f} |")

conf = Counter(); correct = 0
for cid, labels in truth.items():
    e, g = expected_decision(labels), preds[cid]["decision"]; conf[(e, g)] += 1; correct += e == g
acc = correct / len(truth); macro = sum(f1s) / len(f1s)
lines += ["", f"**Decision accuracy:** {acc:.3f} ({correct}/{len(truth)})  ", f"**Macro F1 (rules):** {macro:.3f}", "",
          "## Decision confusion (expected -> got)", ""]
lines += [f"- {e} -> {g}: {n}" for (e, g), n in sorted(conf.items())]
report = "\n".join(lines)
open(f"{a.out}/report.md", "w").write(report); print(report)
if macro < a.min_f1: raise SystemExit(f"Macro F1 {macro:.3f} below threshold {a.min_f1}")

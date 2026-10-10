#!/usr/bin/env python3
"""Run the compliance agent over all claims and write an audit log."""
import argparse, json, os
from collections import Counter
from agent import ComplianceAgent

ap = argparse.ArgumentParser()
ap.add_argument("--data", default="data"); ap.add_argument("--policy", default="policy.json")
ap.add_argument("--out", default="outputs"); ap.add_argument("--llm", action="store_true",
                help="use Claude (needs ANTHROPIC_API_KEY) to write explanations; decisions stay rule-based")
a = ap.parse_args()

load = lambda p: json.load(open(p))
claims, emps, policy = load(f"{a.data}/claims.json"), load(f"{a.data}/employees.json"), load(a.policy)
agent = ComplianceAgent(policy, emps, claims, use_llm=a.llm)

os.makedirs(a.out, exist_ok=True)
counts = Counter()
with open(f"{a.out}/decisions.jsonl", "w") as f:
    for c in claims:
        res = agent.process_claim(c)
        counts[res["decision"]] += 1
        f.write(json.dumps(res) + "\n")
print(f"Processed {len(claims)} claims -> {a.out}/decisions.jsonl")
for k, v in counts.most_common(): print(f"  {k:22s} {v}")

#!/usr/bin/env python3
"""
Policy Compliance Agent - single-file solution (Python 3.9+, standard library only)

Components
  1. POLICY        declarative rule set (9 rules, severities, decision map)
  2. Rule engine   record rules (declarative conditions) + group rules (duplicate, split transaction)
  3. Agent         tool-using loop: lookup -> enrich -> record rules -> group rules (DB query) -> decide -> explain
                   with audit trace per claim. Decisions are deterministic; an LLM may only write explanations.
  4. Synthetic data generator with injected labelled violations, boundary near-misses, duplicates, split claims
  5. Evaluation    per-rule precision/recall/F1, decision accuracy, confusion matrix
  6. Self-tests    boundary checks

Run:  python solution.py            (full pipeline + report)
      python solution.py --n 1000 --seed 7 --save     (also writes data/ and outputs/ files)
      python solution.py --llm      (optional: Claude explanations if ANTHROPIC_API_KEY is set)
"""
import argparse, json, os, random, operator, urllib.request
from collections import defaultdict, Counter
from datetime import date, timedelta

POLICY = json.loads(r'''
{
  "policy_name": "Corporate Expense Policy v1.0",
  "decision_map": {
    "high": "REJECT",
    "medium": "ESCALATE_TO_HUMAN",
    "low": "APPROVE_WITH_WARNING",
    "none": "APPROVE"
  },
  "rules": [
    {"id": "P001", "name": "Receipt required for claims over $25", "severity": "medium", "type": "record",
     "when": [{"field": "amount", "op": "gt", "value": 25}, {"field": "has_receipt", "op": "eq", "value": false}]},

    {"id": "P002", "name": "Meal cap of $75 per person", "severity": "medium", "type": "record",
     "when": [{"field": "category", "op": "eq", "value": "meals"}, {"field": "per_person_amount", "op": "gt", "value": 75}]},

    {"id": "P003", "name": "Self-approval is prohibited", "severity": "high", "type": "record",
     "when": [{"field": "approver_id", "op": "eq", "ref": "employee_id"}]},

    {"id": "P004", "name": "Prohibited vendor / merchant type", "severity": "high", "type": "record",
     "when": [{"field": "text", "op": "contains_any", "value": ["casino", "liquor", "gambling", "strip club"]}]},

    {"id": "P005", "name": "Weekend spend outside travel category", "severity": "low", "type": "record",
     "when": [{"field": "is_weekend", "op": "eq", "value": true}, {"field": "category", "op": "ne", "value": "travel"}]},

    {"id": "P006", "name": "Claim submitted more than 60 days after expense", "severity": "medium", "type": "record",
     "when": [{"field": "days_to_submit", "op": "gt", "value": 60}]},

    {"id": "P007", "name": "Claims over $1000 need approver level >= 4", "severity": "high", "type": "record",
     "when": [{"field": "amount", "op": "gt", "value": 1000}, {"field": "approver_level", "op": "lt", "value": 4}]},

    {"id": "P008", "name": "Duplicate claim", "severity": "high", "type": "group", "check": "duplicate", "params": {}},

    {"id": "P009", "name": "Split transaction to avoid approval threshold", "severity": "high", "type": "group", "check": "split",
     "params": {"single_limit": 200, "total_limit": 500, "min_count": 2}}
  ]
}
''')

# ======================= RULE ENGINE =======================
OPS = {
    "gt": operator.gt, "gte": operator.ge, "lt": operator.lt, "lte": operator.le,
    "eq": operator.eq, "ne": operator.ne,
    "in": lambda a, b: a in b, "not_in": lambda a, b: a not in b,
    "contains_any": lambda a, b: any(k in a for k in b),
}


class ClaimsDB:
    """Tiny in-memory 'database' the agent can query (stands in for a real ledger)."""
    def __init__(self, claims):
        self.idx = defaultdict(list)
        for c in claims:
            self.idx[(c["employee_id"], c["vendor"], c["date"])].append(c)

    def same_day_vendor(self, claim):
        return self.idx[(claim["employee_id"], claim["vendor"], claim["date"])]


def eval_condition(cond, rec):
    left = rec[cond["field"]]
    right = rec[cond["ref"]] if "ref" in cond else cond["value"]
    return OPS[cond["op"]](left, right)


def check_record_rule(rule, rec):
    """Returns evidence dict if violated, else None."""
    if all(eval_condition(c, rec) for c in rule["when"]):
        return {c["field"]: rec[c["field"]] for c in rule["when"]}
    return None


def check_duplicate(rule, rec, db):
    for o in db.same_day_vendor(rec):
        if (o["claim_id"] != rec["claim_id"] and o["amount"] == rec["amount"]
                and (o["submitted_date"], o["claim_id"]) < (rec["submitted_date"], rec["claim_id"])):
            return {"duplicate_of": o["claim_id"]}
    return None


def check_split(rule, rec, db):
    p = rule["params"]
    group = db.same_day_vendor(rec)
    if (len(group) >= p["min_count"] and all(g["amount"] < p["single_limit"] for g in group)
            and sum(g["amount"] for g in group) >= p["total_limit"]):
        return {"group": [g["claim_id"] for g in group], "total": round(sum(g["amount"] for g in group), 2)}
    return None


GROUP_CHECKS = {"duplicate": check_duplicate, "split": check_split}

# ======================= AGENT =======================
SEV_ORDER = ["high", "medium", "low"]


class ComplianceAgent:
    def __init__(self, policy, employees, claims, use_llm=False):
        self.policy = policy
        self.emps = {e["id"]: e for e in employees}
        self.db = ClaimsDB(claims)
        self.use_llm = use_llm
        self.tools = {
            "lookup_employee": self.lookup_employee,
            "enrich": self.enrich,
            "check_record_rules": self.check_record_rules,
            "check_group_rules": self.check_group_rules,
            "decide": self.decide,
            "explain": self.explain,
        }

    # ---------- tools ----------
    def lookup_employee(self, claim):
        return self.emps.get(claim["employee_id"]), self.emps.get(claim["approver_id"])

    def enrich(self, claim, emp, appr):
        d, s = date.fromisoformat(claim["date"]), date.fromisoformat(claim["submitted_date"])
        rec = dict(claim)
        rec.update(
            employee_level=emp["level"], approver_level=appr["level"],
            per_person_amount=round(claim["amount"] / max(claim.get("attendees", 1), 1), 2),
            days_to_submit=(s - d).days, is_weekend=d.weekday() >= 5,
            text=(claim["vendor"] + " " + claim["description"]).lower(),
        )
        return rec

    def _violation(self, rule, evidence):
        return {"rule_id": rule["id"], "name": rule["name"], "severity": rule["severity"], "evidence": evidence}

    def check_record_rules(self, rec):
        out = []
        for r in self.policy["rules"]:
            if r["type"] == "record":
                ev = check_record_rule(r, rec)
                if ev is not None:
                    out.append(self._violation(r, ev))
        return out

    def check_group_rules(self, rec):
        out = []
        for r in self.policy["rules"]:
            if r["type"] == "group":
                ev = GROUP_CHECKS[r["check"]](r, rec, self.db)
                if ev is not None:
                    out.append(self._violation(r, ev))
        return out

    def decide(self, violations):
        sevs = {v["severity"] for v in violations}
        for s in SEV_ORDER:
            if s in sevs:
                return self.policy["decision_map"][s]
        return self.policy["decision_map"]["none"]

    def explain(self, claim, violations, decision):
        if self.use_llm and os.getenv("ANTHROPIC_API_KEY"):
            try:
                return self._llm_explain(claim, violations, decision)
            except Exception:
                pass  # fall back to template
        if not violations:
            return f"{claim['claim_id']} complies with all policy rules."
        parts = "; ".join(f"{v['rule_id']} {v['name']} [{v['severity']}]" for v in violations)
        return f"{claim['claim_id']} -> {decision}. Triggered: {parts}."

    def _llm_explain(self, claim, violations, decision):
        body = {
            "model": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5"), "max_tokens": 200,
            "messages": [{"role": "user", "content":
                "Write a 2-sentence plain-English explanation for a finance reviewer. Do NOT change the decision.\n"
                f"Claim: {json.dumps(claim)}\nDecision: {decision}\nViolations: {json.dumps(violations)}"}],
        }
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
            headers={"content-type": "application/json", "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                     "anthropic-version": "2023-06-01"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)["content"][0]["text"].strip()

    # ---------- agent loop ----------
    def process_claim(self, claim):
        trace = []

        def call(tool, summary, *args):
            res = self.tools[tool](*args)
            trace.append({"step": len(trace) + 1, "tool": tool, "result": summary(res)})
            return res

        emp, appr = call("lookup_employee", lambda r: {"employee_found": bool(r[0]), "approver_found": bool(r[1])}, claim)
        if not emp or not appr:  # branch: bad master data -> escalate, stop early
            v = [{"rule_id": "P000", "name": "Unknown employee/approver in master data",
                  "severity": "medium", "evidence": {}}]
            decision = self.decide(v)
            trace.append({"step": len(trace) + 1, "tool": "decide", "result": decision})
            return {"claim_id": claim["claim_id"], "decision": decision, "violations": v,
                    "explanation": self.explain(claim, v, decision), "trace": trace}

        rec = call("enrich", lambda r: {"added": ["levels", "per_person", "days_to_submit", "is_weekend"]}, claim, emp, appr)
        v = call("check_record_rules", lambda r: [x["rule_id"] for x in r], rec)
        v += call("check_group_rules", lambda r: [x["rule_id"] for x in r], rec)
        decision = call("decide", lambda r: r, v)
        expl = call("explain", lambda r: r[:80], claim, v, decision)
        return {"claim_id": claim["claim_id"], "decision": decision, "violations": v,
                "explanation": expl, "trace": trace}

# ======================= SYNTHETIC DATA =======================
CATS = {"meals": ["Cafe Aroma", "Spice Route", "Urban Bistro", "Green Leaf Kitchen"],
        "travel": ["SkyJet Airlines", "MetroRail", "CityCabs", "Grand Stay Hotel"],
        "supplies": ["OfficeMart", "PaperWorks"], "software": ["CloudSuite Inc", "DevTools Co"],
        "training": ["LearnHub", "SkillForge"], "equipment": ["TechDepot", "GadgetWorld"]}
BAD_VENDORS = ["Lucky Star Casino", "Premium Liquor Mart", "Royal Gambling Lounge"]
DEPTS = ["Engineering", "Sales", "Marketing", "Finance", "Operations"]
START, SPAN = date(2026, 1, 5), 200


def make_employees(rng, n=40):
    emps = []
    for i in range(1, n + 1):
        level = 4 if i <= 4 else 5 if i <= 6 else rng.choice([1, 2, 2, 3, 3])
        emps.append({"id": f"E{i:03d}", "department": rng.choice(DEPTS), "level": level})
    return emps


def pick_approver(rng, emps, emp, min_level=3, max_level=9):
    c = [e for e in emps if e["id"] != emp["id"] and max(emp["level"], min_level) <= e["level"] <= max_level]
    return rng.choice(c)


def rand_date(rng, weekend=False, weekday_only=True):
    while True:
        d = START + timedelta(days=rng.randrange(SPAN))
        if weekend and d.weekday() >= 5: return d
        if not weekend and (not weekday_only or d.weekday() < 5): return d


def base(rng, emps, used, cat=None, weekend=False):
    while True:
        emp = rng.choice(emps)
        c = cat or rng.choices(list(CATS), [30, 20, 15, 12, 10, 13])[0]
        d = rand_date(rng, weekend=weekend, weekday_only=(c != "travel"))
        vendor = rng.choice(CATS[c])
        key = (emp["id"], vendor, d.isoformat())
        if key not in used: break
    used.add(key)
    att, amount = 1, round(rng.uniform(5, 400), 2)
    if c == "meals":
        att = rng.randint(1, 4); amount = round(round(rng.uniform(12, 70), 2) * att, 2)
    elif c in ("travel", "equipment") and rng.random() < 0.25:
        amount = round(rng.uniform(1001, 2500), 2)
    appr = pick_approver(rng, emps, emp, 4 if amount > 1000 else 3)
    return {"claim_id": None, "employee_id": emp["id"], "department": emp["department"], "category": c,
            "amount": amount, "currency": "USD", "date": d.isoformat(),
            "submitted_date": (d + timedelta(days=rng.randint(1, 30))).isoformat(),
            "vendor": vendor, "description": f"{c} expense", "attendees": att,
            "has_receipt": amount > 25 or rng.random() < 0.5, "approver_id": appr["id"]}


def _emp(emps, eid): return next(e for e in emps if e["id"] == eid)


def low_approver(rng, emps, eid, level_eq=None):
    """Approver with level <= 3 (or == level_eq), never the employee themself."""
    c = [e for e in emps if e["id"] != eid and (e["level"] == level_eq if level_eq else e["level"] <= 3)]
    return rng.choice(c)["id"]


def inject(rule, rng, emps, used):
    NON = rng.choice(["supplies", "software", "training"])
    if rule == "P001":
        r = base(rng, emps, used, NON); r["amount"] = round(rng.uniform(30, 200), 2); r["has_receipt"] = False
    elif rule == "P002":
        r = base(rng, emps, used, "meals"); r["attendees"] = rng.randint(2, 4)
        r["amount"] = round(round(rng.uniform(76, 120), 2) * r["attendees"], 2); r["has_receipt"] = True
    elif rule == "P003":
        r = base(rng, emps, used, NON); r["approver_id"] = r["employee_id"]
    elif rule == "P004":
        r = base(rng, emps, used, NON); r["vendor"] = rng.choice(BAD_VENDORS); r["description"] = "client entertainment"
    elif rule == "P005":
        r = base(rng, emps, used, NON, weekend=True)
    elif rule == "P006":
        r = base(rng, emps, used, NON)
        r["submitted_date"] = (date.fromisoformat(r["date"]) + timedelta(days=rng.randint(61, 120))).isoformat()
    elif rule == "P007":
        r = base(rng, emps, used, rng.choice(["travel", "equipment"]))
        r["amount"] = round(rng.uniform(1100, 3000), 2); r["has_receipt"] = True
        r["approver_id"] = low_approver(rng, emps, r["employee_id"])
    return r


def boundary(kind, rng, emps, used):
    """Near-miss records that sit exactly ON a threshold and must NOT be flagged."""
    if kind == "receipt_25":
        r = base(rng, emps, used, "supplies"); r["amount"] = 25.00; r["has_receipt"] = False
    elif kind == "meal_75":
        r = base(rng, emps, used, "meals"); r["attendees"] = 2; r["amount"] = 150.00; r["has_receipt"] = True
    elif kind == "late_60":
        r = base(rng, emps, used, "software")
        r["submitted_date"] = (date.fromisoformat(r["date"]) + timedelta(days=60)).isoformat()
    elif kind == "amount_1000":
        r = base(rng, emps, used, "equipment"); r["amount"] = 1000.00; r["has_receipt"] = True
        r["approver_id"] = low_approver(rng, emps, r["employee_id"], level_eq=3)
    return r


def make_split(rng, emps, used):
    while True:
        emp = rng.choice(emps); vendor = rng.choice(CATS["supplies"]); d = rand_date(rng)
        key = (emp["id"], vendor, d.isoformat())
        if key not in used: break
    used.add(key)
    appr = pick_approver(rng, emps, emp)["id"]; out = []
    for amt in rng.sample(range(17000, 19990), 3):
        out.append({"claim_id": None, "employee_id": emp["id"], "department": emp["department"], "category": "supplies",
                    "amount": amt / 100, "currency": "USD", "date": d.isoformat(),
                    "submitted_date": (d + timedelta(days=rng.randint(1, 5))).isoformat(), "vendor": vendor,
                    "description": "supplies expense", "attendees": 1, "has_receipt": True, "approver_id": appr})
    return out

def generate(n=500, seed=42):
    rng, used = random.Random(seed), set()
    emps = make_employees(rng); items = []
    rules = ["P001", "P002", "P003", "P004", "P005", "P006", "P007"]
    n_viol = int(n * 0.04)
    for rule in rules:
        for _ in range(n_viol): items.append((inject(rule, rng, emps, used), [rule]))
    for kind in ["receipt_25", "meal_75", "late_60", "amount_1000"]:
        for _ in range(max(3, n // 60)): items.append((boundary(kind, rng, emps, used), []))
    for _ in range(max(3, n // 50)):
        for r in make_split(rng, emps, used): items.append((r, ["P009"]))
    clean = [(base(rng, emps, used), []) for _ in range(max(n - len(items), 0))]
    items += clean
    pool = list(range(len(clean))); rng.shuffle(pool)
    for i in pool[:max(3, n // 30)]:
        src = clean[i][0]; cp = dict(src)
        cp["submitted_date"] = (date.fromisoformat(src["submitted_date"]) + timedelta(days=2)).isoformat()
        items.append((cp, ["P008"]))
    items.sort(key=lambda it: (it[0]["submitted_date"], it[0]["employee_id"], it[0]["vendor"], it[0]["amount"]))
    claims, truth = [], {}
    for i, (r, labels) in enumerate(items, 1):
        r["claim_id"] = f"CLM-{i:05d}"; claims.append(r); truth[r["claim_id"]] = labels
    return emps, claims, truth


# ======================= EVALUATION =======================
def expected_decision(labels, policy):
    sev = {r["id"]: r["severity"] for r in policy["rules"]}
    for s in ["high", "medium", "low"]:
        if any(sev[l] == s for l in labels): return policy["decision_map"][s]
    return policy["decision_map"]["none"]


def evaluate(policy, truth, preds):
    lines = ["| Rule | Name | TP | FP | FN | Precision | Recall | F1 |", "|---|---|---|---|---|---|---|---|"]
    f1s = []
    for r in policy["rules"]:
        tp = fp = fn = 0
        for cid, labels in truth.items():
            got = r["id"] in [v["rule_id"] for v in preds[cid]["violations"]]; exp = r["id"] in labels
            tp += got and exp; fp += got and not exp; fn += exp and not got
        p = tp / (tp + fp) if tp + fp else 1.0; rc = tp / (tp + fn) if tp + fn else 1.0
        f1 = 2 * p * rc / (p + rc) if p + rc else 0.0; f1s.append(f1)
        lines.append(f"| {r['id']} | {r['name']} | {tp} | {fp} | {fn} | {p:.3f} | {rc:.3f} | {f1:.3f} |")
    conf, correct = Counter(), 0
    for cid, labels in truth.items():
        e, g = expected_decision(labels, policy), preds[cid]["decision"]; conf[(e, g)] += 1; correct += e == g
    lines += ["", f"Decision accuracy: {correct / len(truth):.3f} ({correct}/{len(truth)})",
              f"Macro F1 (rules):  {sum(f1s) / len(f1s):.3f}", "", "Decision confusion (expected -> got):"]
    lines += [f"  {e} -> {g}: {n}" for (e, g), n in sorted(conf.items())]
    return "\n".join(lines), sum(f1s) / len(f1s)


# ======================= SELF-TESTS =======================
def self_tests():
    emps = [{"id": "E1", "department": "Sales", "level": 2}, {"id": "E2", "department": "Sales", "level": 3},
            {"id": "E3", "department": "Sales", "level": 4}]

    def claim(**kw):
        c = dict(claim_id="C1", employee_id="E1", department="Sales", category="supplies", amount=50.0, currency="USD",
                 date="2026-03-03", submitted_date="2026-03-05", vendor="OfficeMart", description="supplies expense",
                 attendees=1, has_receipt=True, approver_id="E3")
        c.update(kw); return c

    def ids(*cs, idx=0):
        return {v["rule_id"] for v in ComplianceAgent(POLICY, emps, list(cs)).process_claim(cs[idx])["violations"]}

    checks = [
        ("clean claim has no violations", ids(claim()) == set()),
        ("$25.00 without receipt is OK", "P001" not in ids(claim(amount=25.0, has_receipt=False))),
        ("$25.01 without receipt flagged", "P001" in ids(claim(amount=25.01, has_receipt=False))),
        ("self-approval flagged", "P003" in ids(claim(approver_id="E1"))),
        ("60 days late is OK", "P006" not in ids(claim(submitted_date="2026-05-02"))),
        ("61 days late flagged", "P006" in ids(claim(submitted_date="2026-05-03"))),
        ("$1500 with level-3 approver flagged", "P007" in ids(claim(amount=1500.0, approver_id="E2"))),
        ("unknown employee escalates", ComplianceAgent(POLICY, emps, [claim()]).process_claim(claim(employee_id="E999"))["decision"] == "ESCALATE_TO_HUMAN"),
        ("duplicate flags later claim only", "P008" in ids(claim(), claim(claim_id="C2", submitted_date="2026-03-07"), idx=1)
         and "P008" not in ids(claim(), claim(claim_id="C2", submitted_date="2026-03-07"), idx=0)),
        ("split transaction flagged", all("P009" in ids(*[claim(claim_id=f"C{i}", amount=180.0 + i) for i in range(3)], idx=k) for k in range(3))),
    ]
    for name, ok in checks: print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    return all(ok for _, ok in checks)


# ======================= MAIN =======================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=500); ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--llm", action="store_true"); ap.add_argument("--save", action="store_true")
    a = ap.parse_args()

    emps, claims, truth = generate(a.n, a.seed)
    nv = sum(1 for v in truth.values() if v)
    print(f"[1/4] Generated {len(claims)} synthetic claims ({nv} with violations, {len(claims) - nv} compliant)")

    agent = ComplianceAgent(POLICY, emps, claims, use_llm=a.llm)
    results = [agent.process_claim(c) for c in claims]
    preds = {r["claim_id"]: r for r in results}
    print("[2/4] Agent decisions:")
    for k, v in Counter(r["decision"] for r in results).most_common(): print(f"      {k:22s} {v}")

    print("[3/4] Evaluation vs ground truth:\n")
    report, macro = evaluate(POLICY, truth, preds)
    print(report)

    print("\n      Sample audit trace for first violating claim:")
    ex = next(r for r in results if r["violations"])
    print("      " + json.dumps({k: ex[k] for k in ("claim_id", "decision", "explanation", "trace")}, indent=2).replace("\n", "\n      "))

    print("\n[4/4] Self-tests:")
    ok = self_tests()

    if a.save:
        os.makedirs("data", exist_ok=True); os.makedirs("outputs", exist_ok=True)
        for name, obj in [("employees", emps), ("claims", claims), ("ground_truth", truth)]:
            json.dump(obj, open(f"data/{name}.json", "w"), indent=1)
        with open("outputs/decisions.jsonl", "w") as f:
            for r in results: f.write(json.dumps(r) + "\n")
        open("outputs/report.md", "w").write(report)
        print("\nSaved data/ and outputs/")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()

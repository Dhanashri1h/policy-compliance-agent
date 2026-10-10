#!/usr/bin/env python3
"""Synthetic expense-claim generator with injected, labelled policy violations."""
import argparse, json, os, random
from datetime import date, timedelta

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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=500); ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="data"); a = ap.parse_args()
    rng, used = random.Random(a.seed), set()
    emps = make_employees(rng); items = []   # (record, [rule labels])

    rules = ["P001", "P002", "P003", "P004", "P005", "P006", "P007"]
    n_viol = int(a.n * 0.04)                  # per single-record rule
    for rule in rules:
        for _ in range(n_viol): items.append((inject(rule, rng, emps, used), [rule]))
    for kind in ["receipt_25", "meal_75", "late_60", "amount_1000"]:
        for _ in range(max(3, a.n // 60)): items.append((boundary(kind, rng, emps, used), []))
    for _ in range(max(3, a.n // 50)):
        for r in make_split(rng, emps, used): items.append((r, ["P009"]))
    n_clean = a.n - len(items)
    clean = [(base(rng, emps, used), []) for _ in range(max(n_clean, 0))]
    items += clean
    pool = list(range(len(clean)))
    rng.shuffle(pool)
    for i in pool[:max(3, a.n // 30)]:        # duplicates cloned from clean records
        src = clean[i][0]; cp = dict(src)
        cp["submitted_date"] = (date.fromisoformat(src["submitted_date"]) + timedelta(days=2)).isoformat()
        items.append((cp, ["P008"]))

    items.sort(key=lambda it: (it[0]["submitted_date"], it[0]["employee_id"], it[0]["vendor"], it[0]["amount"]))
    claims, truth = [], {}
    for i, (r, labels) in enumerate(items, 1):
        r["claim_id"] = f"CLM-{i:05d}"; claims.append(r); truth[r["claim_id"]] = labels

    os.makedirs(a.out, exist_ok=True)
    for name, obj in [("employees.json", emps), ("claims.json", claims), ("ground_truth.json", truth)]:
        json.dump(obj, open(os.path.join(a.out, name), "w"), indent=1)
    nv = sum(1 for v in truth.values() if v)
    print(f"Generated {len(claims)} claims ({nv} with violations, {len(claims)-nv} compliant) -> {a.out}/")


if __name__ == "__main__":
    main()

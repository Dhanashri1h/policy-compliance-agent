"""Rule engine: declarative record rules + cross-record (group) rules."""
import operator
from collections import defaultdict

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

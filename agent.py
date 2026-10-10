"""Policy compliance agent: tool-using loop with audit trace.
Decisions are ALWAYS made by the rule engine. An LLM (optional) only writes the explanation."""
import json, os, urllib.request
from datetime import date
from rules import check_record_rule, GROUP_CHECKS, ClaimsDB

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

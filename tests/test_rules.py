import unittest, json, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent import ComplianceAgent

POLICY = json.load(open(os.path.join(os.path.dirname(__file__), "..", "policy.json")))
EMPS = [{"id": "E1", "department": "Sales", "level": 2}, {"id": "E2", "department": "Sales", "level": 3},
        {"id": "E3", "department": "Sales", "level": 4}]


def claim(**kw):
    c = dict(claim_id="C1", employee_id="E1", department="Sales", category="supplies", amount=50.0, currency="USD",
             date="2026-03-03", submitted_date="2026-03-05", vendor="OfficeMart", description="supplies expense",
             attendees=1, has_receipt=True, approver_id="E3")
    c.update(kw); return c


def run(*claims):
    ag = ComplianceAgent(POLICY, EMPS, list(claims)); return ag.process_claim(claims[0])


def ids(res): return {v["rule_id"] for v in res["violations"]}


class Tests(unittest.TestCase):
    def test_clean(self): self.assertEqual(run(claim())["decision"], "APPROVE")
    def test_receipt_boundary(self):
        self.assertNotIn("P001", ids(run(claim(amount=25.0, has_receipt=False))))
        self.assertIn("P001", ids(run(claim(amount=25.01, has_receipt=False))))
    def test_self_approval_rejects(self):
        r = run(claim(approver_id="E1")); self.assertIn("P003", ids(r)); self.assertEqual(r["decision"], "REJECT")
    def test_late_boundary(self):
        self.assertNotIn("P006", ids(run(claim(submitted_date="2026-05-02"))))  # exactly 60 days
        self.assertIn("P006", ids(run(claim(submitted_date="2026-05-03"))))     # 61 days
    def test_weekend_is_low(self):
        r = run(claim(date="2026-03-07", submitted_date="2026-03-09"))
        self.assertEqual(r["decision"], "APPROVE_WITH_WARNING")
    def test_approval_level(self):
        self.assertIn("P007", ids(run(claim(amount=1500.0, approver_id="E2"))))
        self.assertNotIn("P007", ids(run(claim(amount=1500.0, approver_id="E3"))))
    def test_unknown_employee_escalates(self):
        r = run(claim(employee_id="E999")); self.assertEqual(r["decision"], "ESCALATE_TO_HUMAN"); self.assertIn("P000", ids(r))
    def test_duplicate(self):
        a = claim(); b = claim(claim_id="C2", submitted_date="2026-03-07")
        ag = ComplianceAgent(POLICY, EMPS, [a, b])
        self.assertNotIn("P008", ids(ag.process_claim(a))); self.assertIn("P008", ids(ag.process_claim(b)))
    def test_split(self):
        cs = [claim(claim_id=f"C{i}", amount=180.0 + i) for i in range(3)]
        ag = ComplianceAgent(POLICY, EMPS, cs)
        self.assertTrue(all("P009" in ids(ag.process_claim(c)) for c in cs))


if __name__ == "__main__": unittest.main()

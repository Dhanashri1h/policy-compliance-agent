# Policy Compliance Agent (rule-based + synthetic data)

Zero dependencies. Python 3.9+.

```bash
make all          # generate data -> run agent -> evaluate -> unit tests
```
or step by step:
```bash
python3 generate_data.py --n 500 --seed 42
python3 run_agent.py                # add --llm (+ ANTHROPIC_API_KEY) for LLM-written explanations
python3 evaluate.py
python3 -m unittest discover -s tests -v
```

## Architecture
- `policy.json`      declarative policy (9 rules, severities, decision map) - change policy without touching code
- `rules.py`         rule engine: record rules (ops: gt/lt/eq/contains_any/field-ref) + group rules (duplicate, split)
- `agent.py`         tool-using agent loop: lookup_employee -> enrich -> record rules -> group rules (DB query) -> decide -> explain; early-exit branch for unknown employee; full audit trace per claim
- `generate_data.py` synthetic claims with injected labelled violations, boundary near-misses, duplicates, split transactions; ground truth stored separately
- `evaluate.py`      per-rule precision/recall/F1, decision accuracy, confusion matrix
- `tests/`           boundary unit tests

## Design principle
Decisions are deterministic and auditable. The LLM (optional) only writes the explanation and can never change the decision.

Outputs: `outputs/decisions.jsonl` (audit log with traces), `outputs/report.md`.

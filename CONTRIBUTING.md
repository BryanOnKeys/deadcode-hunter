# Contributing to deadcode-hunter

Thanks for considering a contribution. deadcode-hunter finds dead code
in Python, Java and TypeScript projects — and proves it's safe to
delete. The analysis engine is deterministic: no network calls, no LLM
in the core pipeline.

## Setup

- Python 3.10+
- pip install -e ".[dev]"
- For Java scanner work: pip install -e ".[dev,java]";
  for TypeScript: pip install -e ".[dev,typescript]"
- Run the suite: python -m pytest tests/ -q

## Workflow

1. Fork the repo and create a branch.
2. Make your change. Every new detection rule needs a fixture case in
   tests/fixtures/sample_repo/ plus a test.
3. Open a pull request against master and describe what changed and
   how you tested it.

## House rules

- Keep the engine deterministic — no network calls, no LLM in the core
  pipeline.
- A candidate is only `high` when there is genuinely no evidence it is
  alive; when in doubt, downgrade, don't delete.
- `prove` trusts the test suite: it proves "the suite stays green
  after deletion" — nothing more.

## Good first issues

Issues labeled `good first issue` are scoped for newcomers — each states
its difficulty and acceptance criteria. Ask in the issue if anything is
unclear.

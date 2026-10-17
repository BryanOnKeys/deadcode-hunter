# deadcode-hunter

> Find dead code in Python projects — **and prove it's safe to delete.**

![build](https://img.shields.io/badge/build-passing-brightgreen)
![python](https://img.shields.io/badge/python-%3E%3D3.10-blue)
![license](https://img.shields.io/badge/license-MIT-green)
![version](https://img.shields.io/badge/version-0.1.0-orange)

Most dead-code detectors (like vulture) do text-level "did this name appear anywhere" matching — cross-file imports, instance methods and decorators quickly turn into false positives or misses. deadcode-hunter does **cross-file reference resolution**: it understands `import` chains, `self` method calls, and method calls after `obj = Klass()`, then grades every candidate with a **confidence level** — only "deleting this is definitely safe" gets marked high.

Zero heavy dependencies (CLI is just typer + rich), the analysis engine uses only stdlib `ast`. **No network, no LLM calls, fully deterministic.**

## Install

```bash
pip install deadcode-hunter
# or from source
pip install -e ".[dev]"
```

Requires Python >= 3.10.

## Usage

### scan — find candidates

```bash
$ deadcode-hunter scan ./src
               deadcode-hunter: 6 candidate(s) in tests/fixtures/sample_repo/dead.py
┏━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ File    ┃ Symbol                     ┃ Kind     ┃ Confidence ┃ Evidence                          ┃
┡━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ dead.py │ dead.os                    │ import   │ high       │ imported but never used           │
│ dead.py │ dead.system                │ import   │ high       │ imported but never used           │
│ dead.py │ dead.DEAD_CONST            │ variable │ high       │ no references found in the        │
│         │                            │          │            │ scanned codebase                  │
│ dead.py │ dead.dead_func             │ function │ high       │ no references found in the        │
│         │                            │          │            │ scanned codebase                  │
│ dead.py │ dead.DeadClass             │ class    │ high       │ no references found in the        │
│         │                            │          │            │ scanned codebase                  │
│ dead.py │ dead.DeadClass.dead_method │ method   │ high       │ no references found in the        │
│         │                            │          │            │ scanned codebase                  │
└─────────┴────────────────────────────┴──────────┴────────────┴───────────────────────────────────┘
high: 6  medium: 0  low: 0
```

```bash
# only "safe to delete"
deadcode-hunter scan ./src --min-confidence high

# machine-readable, for CI / scripts
deadcode-hunter scan ./src --format json --min-confidence high
```

### prove — deletion safety proof

After `scan` finds candidates, `prove` **dynamically verifies** each one: delete it, run your test suite, and only a green suite counts as "proven safe".

```bash
$ deadcode-hunter prove ./src
[1/3] proving mymod.dead_func ...
[2/3] proving mymod.DeadClass ...
[3/3] proving mymod.DeadClass.run ...
    deadcode-hunter prove: /tmp/demo  (python -m pytest -x -q)
┏━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━┓
┃ File     ┃ Symbol              ┃ Result ┃ Detail                ┃
┡━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━┩
│ mymod.py │ mymod.dead_func     │ proven │ tests pass without it │
│ mymod.py │ mymod.DeadClass     │ proven │ tests pass without it │
│ mymod.py │ mymod.DeadClass.run │ proven │ tests pass without it │
└──────────┴─────────────────────┴────────┴───────────────────────┘
proven: 3  breaks: 0  skipped: 0
```

```bash
# prove medium-and-up candidates
deadcode-hunter prove ./src --min-confidence medium

# point at a faster test subset
deadcode-hunter prove ./src --test-cmd "pytest tests/test_utils.py -q"

# machine-readable
deadcode-hunter prove ./src --format json
```

How it works, in one sentence: **isolated git worktrees + your test suite as the deletion safety proof** — every deletion happens in a clean checkout of HEAD, your working tree is never touched; green suite → `proven`, red suite → `breaks` (with a failure summary), too-complex-to-delete → `skipped` with a reason.

Deletion is AST-level: precise `lineno`/`end_lineno` removal (decorator lines included); `from x import a, b` only drops the dead name; hollowing out a class body auto-inserts `pass` before verifying — the same as deleting by hand.

Performance note: each candidate runs the full test suite once, which is slow on large repos. Start small, or use `--test-cmd` to target a test subset.

### report — visual HTML report

A **single self-contained HTML file**: zero external dependencies (all CSS/JS inlined, no CDN), double-click to open in a browser.

```bash
$ deadcode-hunter report ./src --output /tmp/deadcode.html
Report written to /tmp/demo-report.html (3 candidates)
```

```bash
# merge prove results into the report (proven / breaks badges + pass counts)
deadcode-hunter prove ./src --format json > prove.json
deadcode-hunter report ./src --prove-json prove.json
```

The report includes:

- Summary header: total candidates, confidence distribution, per-file breakdown, prove pass counts (if merged)
- Clickable file tree on the left, symbol list on the right: name, kind, color-coded confidence badges, evidence chain
- Click a symbol to expand the source snippet with dead lines highlighted
- Search box to filter by symbol name
- Dark mode follows `prefers-color-scheme`

### Confidence levels

| Level | Meaning | Example |
|-------|---------|---------|
| **high** (deletable) | Zero references in the whole codebase, no reason to be alive | A function never imported, never called |
| **medium** (take a look) | Weak reference evidence, or decorated | A `@app.route` view, an unresolvable `obj.attr` access |
| **low** (probably alive) | Its scope uses `getattr`/`eval`/`__import__` | Plugin-style dynamically loaded code |

Every candidate carries an **evidence chain**: definition location, reference counts, and why it was judged dead/suspicious.

### What it gets right

- Symbols exported via `__all__` are not reported (that's public API)
- Definitions inside `if __name__ == "__main__"` blocks are not reported
- Dunder methods (`__str__`, …) and `@abstractmethod` are not reported
- Decorated functions → downgraded to medium (the decorator may be a framework registration)
- `getattr` / `eval` / `__import__` / string-concatenated imports → enclosing scope downgraded to low
- Test files (`test_*.py`, `tests/` dirs) are analyzed normally, but `test_*` functions and `@pytest.fixture`s are not reported (and marked as test origin)
- Cross-file: `from pkg.mod import foo`, `import pkg.mod` + `pkg.mod.foo()` are chained
- Instance methods: `self._helper()`, `worker = Worker(); worker.run()`, `Worker().run()` all resolve
- Annotations as types: `x.bar()` inside `def f(x: Foo)` resolves to `Foo.bar`
- Respects `.gitignore`; skips `.venv` / `__pycache__` / `.git`

## deadcode-hunter vs vulture

| | vulture | deadcode-hunter |
|---|---|---|
| Reference analysis | Name matching within a single file | **Cross-file** import chains + reference resolution |
| Instance methods | Barely supported (`self._helper` false-positives) | `self` / variable types / constructor chains resolve |
| Output | Dead-code list | Dead-code list + **confidence levels + evidence chain** |
| Safety proof | — | **`prove`**: delete + run your test suite in an isolated git worktree |
| Report | Text list | **Self-contained HTML report** (file tree, evidence, source snippets, dark mode) |
| Dynamic code | Easily misjudged | `getattr`/`eval` scopes auto-downgraded to low |
| Determinism | Deterministic | Deterministic (no LLM, no network) |

In short: vulture answers "this code is *probably* unused"; deadcode-hunter answers "deleting this code is *safe*, and here's the evidence".

## Limitations

Honest ones, so you can trust the rest:

- **It only sees inside the scanned codebase.** A library's public API with no internal callers is reported `high` — that's an honest "no *internal* references". Check for external callers before deleting.
- **`prove` trusts your test suite.** It proves "the suite stays green after deletion" — nothing more. Behavior your tests don't cover is invisible to it.
- **No full type inference.** `x.attr` where `x` is an unannotated local or loop variable is recorded as a weak reference (medium) — a deliberate conservative choice.
- **It doesn't execute code.** `__getattr__`, metaclass magic, and calls inside C extensions are invisible.

## Roadmap

- ✅ **M1 — cross-file scanner** (done): reference resolution across files, high/medium/low confidence with evidence chains.
- ✅ **M2 — `prove`** (done): deletion safety proof via isolated git worktrees + your test suite.
- ✅ **M3 — HTML report** (done): single self-contained file, file tree, evidence, source snippets, dark mode, `--prove-json` merge.
- **TypeScript support**: bring the same "cross-file references + confidence" model to TS (on top of tsc symbol resolution).
- **LLM-assisted investigator**: an optional LLM pass over low-confidence candidates to suggest *why* something might still be alive. The static engine stays deterministic — the LLM only advises, never decides.

## Contributing

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -q
```

PRs are welcome. Two house rules:

1. Keep the analysis engine deterministic — no network calls, no LLM in the core pipeline.
2. Add a fixture case in `tests/fixtures/sample_repo/` for every new detection rule, plus a test.

## License

MIT — see [LICENSE](LICENSE).

---

中文文档：[README.zh-CN.md](README.zh-CN.md)

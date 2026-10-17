# deadcode-hunter

> Find dead code in Python and Java projects — **and prove it's safe to delete.**

![build](https://img.shields.io/badge/build-passing-brightgreen)
![python](https://img.shields.io/badge/python-%3E%3D3.10-blue)
![license](https://img.shields.io/badge/license-MIT-green)
![version](https://img.shields.io/badge/version-0.3.0-orange)

Most dead-code detectors (like vulture) do text-level "did this name appear anywhere" matching — cross-file imports, instance methods and decorators quickly turn into false positives or misses. deadcode-hunter does **cross-file reference resolution**: it understands `import` chains, `self` method calls, and method calls after `obj = Klass()`, then grades every candidate with a **confidence level** — only "deleting this is definitely safe" gets marked high.

Zero heavy dependencies (CLI is just typer + rich), the analysis engine uses only stdlib `ast`. **No network, no LLM calls, fully deterministic.**

## Install

```bash
pip install deadcode-hunter
# with Java support (tree-sitter based scanner)
pip install "deadcode-hunter[java]"
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

### clean — one-command cleanup

`prove` tells you a deletion is safe; `clean` finishes the job: it scans,
proves every candidate in an isolated git worktree, and applies the
proven-safe deletions to your working tree. Changes are left uncommitted —
review with `git diff` and commit when happy.

```bash
$ deadcode-hunter clean . --dry-run
 deadcode-hunter clean: 3 proven-safe deletion(s) in .
┏━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┓
┃ File       ┃ Symbol                      ┃ Kind     ┃
┡━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━┩
│ billing.py │ billing.legacy_discount     │ function │
│ billing.py │ billing.LegacyReport        │ class    │
│ billing.py │ billing.LegacyReport.render │ method   │
└────────────┴─────────────────────────────┴──────────┘
Dry run: no files were changed.
```

Safety rails: the target must be a git repo with a clean working tree
(commit or stash first — `clean` refuses otherwise), deletions reuse the
same AST-driven logic as `prove`, and you confirm the list unless you pass
`--yes`.

```bash
deadcode-hunter clean . --yes            # apply without asking
deadcode-hunter clean . --format json    # machine-readable plan + result
```

### Baseline mode — only new dead code

One-off cleanups are nice; never *adding* new dead code is nicer. Save a
baseline once and commit it, then gate CI on it:

```bash
$ deadcode-hunter scan . --save-baseline baseline.json --min-confidence high
Baseline saved to baseline.json (3 candidates)
```

```bash
$ deadcode-hunter scan . --baseline baseline.json --min-confidence high
                      deadcode-hunter: 1 candidate(s) in .
┏━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━┓
┃ File       ┃ Symbol             ┃ Kind     ┃ Confidence ┃ Evidence           ┃
┡━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━┩
│ billing.py │ billing.another_l… │ function │ high       │ no references      │
│            │                    │          │            │ found in the       │
│            │                    │          │            │ scanned codebase   │
└────────────┴────────────────────┴──────────┴────────────┴────────────────────┘
high: 1  medium: 0  low: 0
```

Only candidates absent from the baseline are reported. `--fail-on-new`
exits 1 when anything (new) is found — the CI gate:

```bash
deadcode-hunter scan src --min-confidence high --fail-on-new
```

### Configuration file

Put project defaults in the scanned project's own `pyproject.toml`
(only that file is read — never parent directories):

```toml
[tool.deadcode-hunter]
exclude = ["tests/fixtures/**", "docs/**"]  # glob on the scan-root-relative path
entry-points = ["mypackage.api.*"]          # glob on the symbol qualname: never reported
min-confidence = "high"                     # default for scan / prove / clean
test-cmd = "pytest -q"                      # default test command for prove / clean
```

CLI flags always override config values.

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

## Java support

The Java engine is built on [tree-sitter](https://tree-sitter.github.io/) and feeds the **same pipeline** — same confidence levels, evidence chains, `prove`, and HTML reports. Language is detected by file extension (`.py` → Python, `.java` → Java); directories mixing both scan both. Override with `--lang`:

```bash
deadcode-hunter scan ./my-java-project
deadcode-hunter scan ./mixed --lang java   # only .java files
deadcode-hunter scan ./mixed --lang python # only .py files
```

Java-specific rules:

- `main` methods (and the class containing one) are entry points — never reported
- `@Override` methods are treated as alive by construction (wired to their supertype)
- Other annotations (e.g. `@Test`) downgrade to medium, like Python decorators
- `private` members with no references → high; `public` ones → high with a *"may be a public API"* note in the evidence chain
- Reflection string literals (`Class.forName("com.foo.Bar")`, `getMethod("name")`) downgrade their targets to medium instead of high
- Unused imports → high; `import pkg.*` is never flagged (the names it provides are unknowable, so flagging it would be a false positive)
- Constructors are tracked: `new Foo()` keeps both the class and its constructor alive

For `prove` on Java projects, point `--test-cmd` at your own build:

```bash
deadcode-hunter prove ./my-java-project --test-cmd "mvn -q test"
# or: deadcode-hunter prove ./my-java-project --test-cmd "gradle test"
```

Java-specific limitations: no full type inference (same conservative weak-reference rule as Python); annotation processors and DI frameworks (Spring, Dagger, …) wire code invisibly — treat `high` on framework-managed beans with suspicion.

## GitHub Action

Use deadcode-hunter in CI with the composite action (`action.yml`):

```yaml
- uses: zheng0629/deadcode-hunter@v0.3.0
  with:
    path: .
    min-confidence: high
```

| Input | Default | Description |
|---|---|---|
| `path` | `.` | Directory (or file) to scan |
| `min-confidence` | `high` | Minimum confidence: high, medium or low |
| `pip-version` | `0.3.0` | deadcode-hunter version to install from PyPI |

The action exposes a `findings` output (`total=3 high=2 medium=1 low=0`).
See [`examples/pr-comment.yml`](examples/pr-comment.yml) for a copy-paste
workflow that comments the findings table on every pull request (creates the
comment once, then updates it in place).

This repo dogfoods it: [`.github/workflows/ci.yml`](.github/workflows/ci.yml)
runs the test suite and then `scan src --min-confidence high --fail-on-new`
as a gate.

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
- ✅ **Java support** (done): tree-sitter based scanner (optional `pip install "deadcode-hunter[java]"`), same pipeline — confidence levels, evidence chains, `prove`, HTML reports; `--lang auto|python|java` to select.
- ✅ **M6 — `clean`** (done): scan → prove → apply proven-safe deletions to the working tree (`--dry-run` preview, `--yes` to skip confirmation, dirty tree refused).
- ✅ **M7 — GitHub Action** (done): composite `action.yml` + copy-paste PR-comment workflow example + this repo's own CI with a `--fail-on-new` dogfood gate.
- ✅ **M8 — baseline mode** (done): `--save-baseline` / `--baseline` report only new dead code; `--fail-on-new` exits 1 for CI.
- ✅ **M9 — config file** (done): `[tool.deadcode-hunter]` in the scanned project's `pyproject.toml` — `exclude`, `entry-points`, `min-confidence`, `test-cmd` defaults.
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

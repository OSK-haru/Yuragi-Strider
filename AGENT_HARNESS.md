# Codex / Claude work harness

This is the shared operating path for project tasks. `AGENTS.md` owns safety and
repository constraints; `DEVELOPMENT_PROTOCOL.md` owns working principles;
`PROJECT_STYLE.md` owns conversation style. Product behavior and progress belong
in the relevant project documents, not here.

For the seven-role audit fleet, follow `AUDIT_FLEET.md`. The earlier single-role
design review contract is in `MONITOR_REVIEW.md`.

## Start a task

1. Read the entry instructions (`AGENTS.md`, plus `CLAUDE.md` in Claude) and the
   common files they reference. Read a nested `AGENTS.md` when working in its
   directory.
2. Run `git status --short` and inspect the affected files. Preserve existing
   uncommitted changes while making the requested edit.
3. State the requested outcome and the smallest affected area. Separate known
   behavior, assumptions, proposals, and decisions. For review or consultation,
   report findings without changing implementation or accepted decisions.
4. Locate the source of truth before editing: runtime code and tests for current
   behavior, `docs_for_develop/README.md` for the implementation index, and the
   relevant design document for requirements and open decisions. `SPEC_MVP.md`
   records the historical MVP, not the current scope.

## Work and verify

- Make the smallest coherent change. Keep simulation code in `core/`, API behavior
  in `api/`, UI behavior in `frontend/`, and independent physics validation in
  `validation_*`. QuTiP, SciPy, and Matplotlib are validation tools, not the
  production compute path.
- When working as a vertical slice, keep `DEVELOPMENT_PROTOCOL.md` item 11 in
  view throughout: do not let the slice's needs shrink extensibility or
  flexibility of the production design, and report any such trade-off at handoff.
- Choose checks from the affected behavior. A passing check supports only the
  behavior it exercises. Record the command and result; do not report a design
  review or documentation edit as runtime validation.
- Use the existing commands from the repository root (PowerShell examples):

  ```powershell
  .\.venv\Scripts\python.exe scripts/run_tests.py fast
  .\.venv\Scripts\python.exe scripts/run_tests.py gate-aware
  .\.venv\Scripts\python.exe scripts/run_tests.py pulse
  .\.venv\Scripts\python.exe scripts/run_tests.py full-audit
  ```

  `fast` is the short core/API check. Use `gate-aware` for circuit and gate-aware
  simulation changes, `pulse` for pulse API changes, and `full-audit` for a
  release-level check; it includes long validation runs. For a narrow change,
  run its relevant `tests.test_*` module directly with
  `.\.venv\Scripts\python.exe -m unittest tests.test_example` (replace the module).

- For frontend changes, run these commands from `frontend/`:

  ```powershell
  npm run lint
  npm run build
  ```

  Run `npm run validate:pulse-lab` or `npm run validate:result-export` when those
  flows change. Do not install packages as part of verification without approval.

- `scripts/verify_cached.py` can reuse prior results for its named checks. A
  cached `SKIP` is evidence of an unchanged fingerprint, not a fresh test run;
  use `--force` when a fresh run is needed. Its check list is not a substitute
  for selecting tests relevant to the task.
- If a tool or dependency is unavailable, report the exact missing check and
  continue with checks that can run locally. Do not silently treat a skipped
  check as passed.

## Finish and hand off

Report the outcome, changed files, checks run with pass/fail/skip status, and
remaining risks or decisions, including any generality given up for a vertical
slice. Keep the report short. When handing work between
Codex and Claude, the receiving agent checks the diff and evidence itself;
neither agent treats the other's summary as a source of truth.

Update the relevant project document only when the task changes a requirement,
design decision, or recorded progress. Do not put transient task notes into
`AGENTS.md`, `CLAUDE.md`, `DEVELOPMENT_PROTOCOL.md`, or `PROJECT_STYLE.md`.

# AGENTS.md — Stinger

This file provides repo-specific guidance for agentic coding assistants.

Repo summary: Python + PyQt6 UI, state machine logic (`transitions`), optional hardware
integration (NI-DAQmx, serial), SQL Server access via SQLAlchemy/pyodbc.

Note on editor/agent rules:
- No `.cursorrules` found.
- Workspace rules may live under `.cursor/rules/` (not committed; see `.gitignore`).
- No `.github/copilot-instructions.md` found.

---

## Environment / Setup

This repo is typically run from a virtualenv located at `.venv/`.

- Create/activate venv (Windows PowerShell):
  - `python -m venv .venv`
  - `.\.venv\Scripts\Activate.ps1`
- Install dependencies:
  - `python -m pip install -r requirements.txt`

Configuration:
- **Production / stand PCs:** `C:\Stinger\` with `stinger_config.yaml` and `quality_cal_config.yaml`
  (set machine `STINGER_CONFIG_DIR=C:\Stinger`). See `docs/DEPLOYMENT.md`.
- **Legacy:** `%LOCALAPPDATA%\Stinger\<STAND_ID>\` still resolved if present.
- **Development fallback:** repo-root YAML if no install copy exists.
- Logs default to `<config_dir>/logs/`.
- Shared builds/docs on `Z:\Engineering\Program Builds\Python Builds\Stinger\` (set `STINGER_RELEASE_ROOT`).

---

## Run / Build Commands

There is no separate “build” step; running is `python run.py`.

- Run the application:
  - `python run.py`

---

## Test Commands (pytest)

The repo uses `pytest` (and has `pytest-qt` available for Qt/UI tests).

- Run all tests:
  - `python -m pytest`
- Run tests quickly/quietly:
  - `python -m pytest -q`
- Run a single file:
  - `python -m pytest tests/test_state_machine.py`
- Run a single test (node id):
  - `python -m pytest tests/test_state_machine.py::TestPortStateMachine::test_initial_state -q`
- Run tests matching a substring:
  - `python -m pytest -k state_machine -q`
- Stop on first failure:
  - `python -m pytest -x`

Qt-specific testing tips:
- Prefer `pytest-qt` fixtures (e.g. `qtbot`) for widgets/signals.
- Avoid tests that require real hardware/DB by default.

---

## Lint / Format / Typecheck

No formatter/linter/typechecker is currently configured in the repo (no `pyproject.toml`,
`setup.cfg`, `tox.ini`, `pytest.ini`). If you add one, keep it lightweight and consistent.

Recommended (optional) tooling if/when adopted:

- Ruff (lint + import sorting):
  - Install: `python -m pip install ruff`
  - Run: `python -m ruff check .`
  - Fix: `python -m ruff check . --fix`

- Black (format):
  - Install: `python -m pip install black`
  - Run: `python -m black .`

- Mypy (types):
  - Install: `python -m pip install mypy`
  - Run: `python -m mypy app`

If you introduce these tools, document exact versions and configuration.

---

## Code Style Guidelines

### Python version / typing
- Target Python 3.10+ (see `requirements.txt`).
- Use type hints on public functions/methods.
- Prefer concrete types (`dict[str, Any]` / `list[str]`) when reasonable.
- Use `Optional[T]` (or `T | None` if the project standardizes on 3.10+ union syntax).
- Avoid `Any` where a small TypedDict/dataclass/Enum would clarify intent.

### Imports
- Group imports in this order with a blank line between groups:
  1) standard library
  2) third-party (PyQt6, SQLAlchemy, transitions, etc.)
  3) local (`from app...`)
- Prefer absolute imports from `app.*` (consistent with `run.py`).
- Avoid wildcard imports.

### Formatting
- Follow PEP 8.
- Indentation: 4 spaces.
- Strings: prefer single quotes inside Python when not user-facing; use f-strings for
  interpolation.
- Keep lines reasonably short (aim ~100 chars) unless breaking harms readability.
- Keep docstrings for modules/classes/public methods; match existing style (triple quotes,
  short summary, optional Args/Returns/Raises).

### Naming
- Modules/files: `snake_case.py`.
- Classes: `CamelCase`.
- Functions/methods: `snake_case`.
- Constants: `UPPER_SNAKE_CASE`.
- Qt signals: `snake_case` (matches existing e.g. `button_state_changed`).
- Enums:
  - Enum type: `CamelCase` (e.g. `PortState`).
  - Members: `UPPER_SNAKE_CASE`.

### Logging
- Prefer `logging.getLogger(__name__)` per module.
- Use `logger.debug/info/warning/error` instead of `print`.
- Exceptions:
  - At boundaries (e.g. `run.py`), it’s acceptable to `print` a concise fatal error before
    exit.
  - Inside libraries/services, log and raise exceptions to preserve stack traces.

### Error handling
- Don’t catch broad `Exception` unless you’re at an application boundary or you can add
  actionable context.
- Preserve the original exception context when re-raising (use `raise ... from e`).
- Validate external inputs early:
  - YAML config (`app/core/config.py`) should raise `ValueError` for missing sections.
  - UI inputs should be parsed/validated before driving hardware/state transitions.

### PyQt / UI architecture
- Keep UI code in `app/ui/` focused on presentation.
- Keep business logic/state transitions in services (`app/services/...`).
- Avoid blocking the Qt event loop:
  - Long-running hardware/DB work should be async/off-thread or chunked via timers.
- Prefer Qt signals/slots for cross-layer communication rather than direct widget mutation.

### State machines
- State machine triggers are string-based; treat them as part of the public API.
- Prefer adding new triggers/states in one place and updating tests accordingly.
- Use `PortState` / `PortSubstate` Enums rather than raw strings except where the
  `transitions` library requires string values.

### Database
- Keep DB I/O behind `app/database/`.
- Do not embed SQL in UI code.
- Make offline mode possible when DB init fails (current behavior logs a warning).
- **IGNORE ControlPressure1-5**: These legacy fields from `ProductTestParameters` are not needed and should never be used by Stinger. They are operational Alicat setpoints that are deliberately excluded from calculations.

### Hardware
- Hardware access belongs under `app/hardware/`.
- Never require physical hardware for unit tests.
- When adding hardware integrations, provide a fake/mock path when feasible for testing.

---

## Testing Guidelines

- Tests live in `tests/` and use `pytest`.
- Prefer small, deterministic tests.
- Avoid time-based flakiness; if you must use timing, keep margins generous.
- For Qt-related tests, use `pytest-qt` and avoid opening real dialogs/windows unless
  required.

---

## Safe Changes / Common Pitfalls

- Do not commit or log secrets:
  - DB credentials might be referenced by `stinger_config.yaml` or environment.
- Be careful editing `stinger_config.yaml`: it is treated as authoritative.
- Keep changes minimal and localized; avoid large refactors unless requested.

---

## Multi-stand workflow (critical)

Several physical stands share one GitHub repo (`Wasco-Engineering/SPS-Calibration-Stand`).
Fixes land on `main` from whichever stand is working that day. **Fetch/pull often** —
do not assume this PC is up to date.

### This machine (CA reference stand)

| Item | Value |
|------|--------|
| Hostname | `CA-MAN-SPS-02` |
| Stand / `equipment_id` | **`CA-SPS-02`** (detail rows: `CA-SPS-02-L` / `CA-SPS-02-R`) |
| Env (preferred) | `STINGER_STAND_ID=STINGER_01`, `STINGER_CONFIG_DIR=C:\Stinger` |
| Config dir | `C:\Stinger` |
| Typical peers | Idaho `ID-SPS-01` / `ID-SPS-02`; CA `CA-SPS-01` |

Legacy labels `STINGER_01` may still appear in env on this PC. The DB equipment ID is
**`CA-SPS-02`**; the env stand ID can remain `STINGER_01` as it only drives local paths.

### What is shared vs stand-local

**Take from `origin/main` (code):**
- `app/**`, `tests/**`, scripts, docs, frozen `.exe` when rebuilding
- Behavioral fixes: vent/equalize, shared-line Alicat serialization, background
  Alicat polling, precision edge gating, cycle-prep, DB equipment-ID handling

**Never overwrite blindly from another stand’s commit (config):**
- `test_parameters.equipment_id` — must stay **`CA-SPS-02`** here
- `hardware.alicat.baudrate` / COM addresses — must match *this* Alicat wiring
- `open_fitting`, `transducer_installed`, switch COM polarity — bench-specific
- Alicat / transducer `*_error_model` blocks — quality-cal for *this* hardware
- `install_manifest.json` hostname / `stand_id`

When merging `origin/main`, resolve conflicts by **keeping this stand’s site YAML
identity** and **taking incoming application code**. After merge, re-check:

```text
equipment_id, baudrate, transducer_installed, COM ports
timing.alicat_background_polling_enabled / alicat_background_poll_hz
```

### Git hygiene between stands

1. **`git fetch origin` frequently** (start of session and before any merge).
2. Before pull/merge: stash or commit local WIP (`stinger_config.yaml` often dirty).
3. Prefer merging `origin/main` into the stand branch, not resetting
   onto another stand’s config.
4. Push stand fixes when they are intentional and tested so peers can pull them —
   do not leave working calibration fixes only on the local disk.
5. Binary `.exe` conflicts: take theirs or rebuild locally; source of truth is Python.

### Cross-stand calibration comparison

- Same **shop order** / part / sequence can be run on multiple stands; compare DB
  `OrderCalibrationDetail` rows (e.g. `IncreasingActivation` =
  increasing-pressure edge, `DecreasingDeactivation` = decreasing-pressure edge).
- **Different serial numbers are different switches** — do not treat SN1 vs SN3 as
  a stand bias unless the operator moved the same DUT.
- Absolute Torr/mmHg parts: site baro is locked for the run but should not shift
  absolute display conversion; small Torr deltas (~1–2) are often Alicat absolute
  bias (~0.02–0.04 PSI), not missing YAML offsets.
- Headless hardware check (right port example):

```text
python scripts/run_executor_headless.py --part 17025 --sequence 399 --port port_b --num-cycles 3
python run.py
```

### Polling / shared Alicat line

Both ports usually share one FTDI COM (addresses A/B). Current code serializes
Alicat ops and can run background cache polling (`timing.alicat_background_*`).
When bringing main onto this stand, keep those timing keys enabled if present on
main, without importing another stand’s `equipment_id` or baro.

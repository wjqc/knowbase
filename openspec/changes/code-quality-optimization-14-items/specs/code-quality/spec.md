# Spec: Code Quality Improvements

## MODIFIED Requirements

### Requirement: REQ-Q-P0-1: Split `_main()` into Composable Functions

**Status**: MODIFIED

The `_main()` function in `knowbase/__main__.py` (L911-L1647, approximately 736 lines) SHALL be refactored into three separate functions:

- `_parse_args(argv) -> argparse.Namespace` — builds the argument parser and returns parsed args
- `_load_config(args) -> dict` — loads and validates project configuration
- `_dispatch(args, config) -> int` — routes to the appropriate subcommand handler, returns exit code

`_main()` itself becomes a thin orchestrator: `_parse_args` -> `_load_config` -> `_dispatch` -> return exit code.

#### Scenario: CLI invocation with valid arguments
- **Given** `knowbase init --scope myproject` is invoked
- **When** `_main()` executes
- **Then** `_parse_args` returns the parsed namespace, `_load_config` returns the config dict, `_dispatch` executes the init handler, and exit code 0 is returned

#### Scenario: CLI invocation with invalid arguments
- **Given** `knowbase` is invoked with no subcommand
- **When** `_main()` executes
- **Then** argparse emits usage and exits with non-zero code, without proceeding to `_load_config` or `_dispatch`

#### Scenario: Dispatching to subcommand handler
- **Given** args contain `cmd="search"`
- **When** `_dispatch` routes the command
- **Then** the search handler executes with the parsed args and loaded config

---

### Requirement: REQ-Q-P0-5: Refine Exception Handling in gitops.py

**Status**: MODIFIED

All `except Exception` blocks in `knowbase/gitops.py` SHALL be refined to catch specific exception types or at minimum include structured logging of the actual error type and message. The following specific locations are targeted:

- L97: `_run_git()` inner try/except — SHALL distinguish `subprocess.CalledProcessError`, `subprocess.TimeoutExpired`, `OSError`
- L163: `commit_all()` — SHALL distinguish git-specific errors from I/O errors
- L184: `commit_paths()` — SHALL distinguish git-specific errors from I/O errors
- L231, L243, L282: push/rebase error handlers — SHALL preserve existing retry logic while adding specific exception types
- L288, L298, L308: sync error handlers — SHALL be refined similarly
- L445, L454, L458, L463: fetch/merge error handlers — SHALL be refined
- L477, L481, L507, L511, L516: integration error handlers — SHALL be refined
- L543, L547, L552: integration recovery handlers — SHALL be refined
- L600, L634, L646, L668, L677, L704, L763, L779, L804, L892: various error handlers — SHALL be refined

#### Scenario: Git command fails due to auth error
- **Given** git push fails with HTTP 403
- **When** the error is caught in the push retry loop
- **Then** `_is_auth_error` check still works, error is recognized as auth, and no retry is attempted

#### Scenario: Git command fails due to network timeout
- **Given** git fetch times out
- **When** the error is caught
- **Then** exponential backoff retry logic still applies, and the error is recorded as network type

#### Scenario: Unexpected non-git error occurs
- **Given** an `OSError` (disk full) occurs during git operation
- **When** caught by refined handler
- **Then** the error is logged with its specific type, and the operation fails cleanly without silent swallowing

---

### Requirement: REQ-Q-P1-6: Refine Exception Handling in retrieval_state.py

**Status**: MODIFIED

Two bare exception handlers in `knowbase/retrieval_state.py` SHALL be refined:

- L35: `except Exception:pass` — SHALL catch specific exceptions (`sqlite3.OperationalError`, `sqlite3.ProgrammingError`), with optional warning-level log
- L72: `except Exception:conn.rollback();raise` — SHALL catch `sqlite3.Error` specifically (which covers OperationalError, IntegrityError, etc.)

#### Scenario: Job lease expires during retrieval
- **Given** the job lease worker encounters a stale lease
- **When** the operation fails due to database contention
- **Then** `sqlite3.OperationalError` is caught, the worker proceeds silently (this is expected behavior)

#### Scenario: Unexpected error during index operation
- **Given** a transaction fails with `sqlite3.Error`
- **When** caught in the refined handler on L72
- **Then** `conn.rollback()` is called and the error is re-raised

---

### Requirement: REQ-Q-P1-8: Extract Common try/except/return Skeleton in __main__.py

**Status**: MODIFIED

Multiple subcommand dispatch paths in `knowbase/__main__.py` (48 try blocks per grep) share a common try/except/return skeleton that SHALL be extracted into a reusable decorator or context manager. Current pattern:

```python
try:
    # subcommand-specific logic
    return 0
except SomeError as e:
    print(f"error: {e}", file=sys.stderr)
    return 1
```

This SHALL be extracted into a reusable decorator or context manager:

```python
@subcommand_handler
def _cmd_search(args, cfg):
    ...
```

OR a context manager:

```python
def run_subcommand(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except SomeError as e:
        ...
```

#### Scenario: Subcommand succeeds
- **Given** a subcommand decorated with `@subcommand_handler` executes successfully
- **When** the function returns normally
- **Then** exit code 0 is propagated

#### Scenario: Subcommand fails with known error
- **Given** a subcommand encounters a `ValueError`
- **When** the error is caught by the handler
- **Then** `sys.exit(1)` is called with a formatted error message to stderr

#### Scenario: Subcommand fails with unexpected error
- **Given** a subcommand encounters an unhandled exception
- **When** the error bubbles up
- **Then** the general error handler catches it, logs with traceback, and returns exit code 2

---

### Requirement: REQ-Q-P1-10: Batch SequenceMatcher in find_similar()

**Status**: MODIFIED

`find_similar()` in `knowbase/store.py` (L297-307) currently iterates over entries and computes `SequenceMatcher.ratio()` per-entry with no early termination. The function SHALL be optimized with the following approach:

- For short texts (< 200 chars): quick pre-filter using `difflib.SequenceMatcher` with `quick_ratio()` before full `ratio()`
- For many candidates: sort by `quick_ratio()` descending, then compute full `ratio()` only for top N candidates

#### Scenario: Finding similar entries with many candidates
- **Given** 1000 entries in the store
- **When** `find_similar()` is called with a query text
- **Then** entries are pre-filtered by `quick_ratio()`, full `ratio()` is computed only for top candidates (e.g., top 50 or those with quick_ratio > 0.4), and results are sorted by full ratio

#### Scenario: Finding similar entries with few candidates
- **Given** 5 entries in the store
- **When** `find_similar()` is called with a query text
- **Then** full `ratio()` is computed for all candidates (overhead of pre-filter is negligible)

#### Scenario: Threshold lower than any candidate
- **Given** all entries have quick_ratio < 0.2 with the threshold at 0.6
- **When** `find_similar()` is called
- **Then** the function returns early without computing full ratio for any candidate
# Contributing

Thanks for looking. Issues and pull requests are welcome; this page says how the code is organised and what a change needs so a review goes quickly.

## Setup

```sh
git clone https://github.com/justmytwospence/garmin-mcp.git && cd garmin-mcp
uv sync --group docs
uv run pytest -q          # about 4 minutes
uv run mkdocs serve       # the documentation at http://127.0.0.1:8000
```

Python 3.12, [uv](https://docs.astral.sh/uv/) for everything. The tests never call Garmin: the mirror tests run on scrubbed real payloads in `test/fixtures/payloads.json`, the engine and server tests against a fake Garmin (`test/fake.py`) that serves those fixtures, and FIT decoding on files synthesised by `test/fitgen.py`. Keep it that way: a test that needs a Garmin account cannot run in CI.

## Layout

| Path | What |
|---|---|
| `src/garmin_mcp/garmin/` | `client.py` (the paced, sequential client and its error classes), `auth.py` (token login, failure classification), `datasets.py` (the registry of every endpoint the mirror keeps), `import_export.py` (Garmin's account export) |
| `src/garmin_mcp/db/` | `tables.py` and `tabledef.py` (the typed schema, declared once), `projections.py` (raw payload to typed rows, and the drift check), `schema.py` (migrations), `raw.py` (landing payloads), `database.py` (the one connection, the write lock, fatal-error handling) |
| `src/garmin_mcp/fit/` | `tables.py` (the FIT schema) and `decode.py` (FIT SDK to tables) |
| `src/garmin_mcp/sync/` | `engine.py` (queue, planner, scheduler, worker) and `fetchers.py` (one Garmin request each) |
| `src/garmin_mcp/mirror.py` | Cache-first reads and write-then-refetch for the tools |
| `src/garmin_mcp/tools/` | The tools, grouped by area; `base.py` is the registry and the provenance block; the vendored upstream modules keep their structure |
| `src/garmin_mcp/http.py`, `snapshot.py`, `cli.py`, `auth_cli.py` | The HTTP surface, the snapshot publisher, the entry points |
| `deploy/` | The compose profiles' files (agentgateway config, Grafana provisioning, the notebook) |
| `docs/`, `scripts/` | The documentation site and the generators of its reference pages |

## Making a change

- **A new column** on a typed table: add it in `db/tables.py` (or `fit/tables.py`) with its type, comment and unit, and append an `ALTER TABLE` migration in `db/schema.py` that also rebuilds the table's rows from `raw_payload` (`project(tx, type, None)`). Never edit a shipped migration. `test_every_typed_table_matches_its_definition` catches a definition that drifted from the live table, and `test_no_field_garmin_returned_is_left_unmapped` catches a fixture key with no column.
- **A new dataset**: a `Dataset` in `garmin/datasets.py`, a projection in `db/projections.py` with its `Shape`s for the drift check, a fixture, and `test_every_dataset_has_a_projection` keeps you honest.
- **A new tool**: a function in the right `tools/` module registered with `@reg.tool(annotations=...)`; read tools return JSON-able data, writes return a `WriteResult`. Then `uv run python scripts/gen_tools.py` and commit the regenerated `docs/reference/tools.md` (CI checks it). Tool names follow upstream where one exists.
- **Schema or FIT changes**: `uv run python scripts/gen_schema.py` and commit `docs/data/schema.md`. Bump `PARSER_VERSION` in `fit/decode.py` when the decoder's output changes; stored files are decoded again automatically.
- **Docs**: `mkdocs build --strict` must pass; links and anchors are validated.

Memory matters: the server runs with a 512 MB DuckDB limit in a 1.5 GB container. Projections must read only the payloads in scope (see `scoped_raw` in `projections.py`), wide projections parse each body once (`_parse_once`), and whole-type scans sample (`live_bodies`). `test/test_memory.py` has the regression tests; `docs/operations.md` the measurements.

## Commits and releases

Conventional commits, one logical change each: `feat:`, `fix:`, `docs:`, `test:`, `build:`, `chore:`. No emojis. Releases are tagged `vX.Y.Z` after a `chore: release X.Y.Z` commit that bumps `pyproject.toml` and `uv.lock` and moves the changelog entry out of *Unreleased*; the tag builds and publishes the image.

## Reporting problems

Use the issue templates. For a sync or login problem, include the `login` and `recent_errors` parts of `garmin_sync_status` and the relevant log lines, with the display name and any identifiers removed. Never attach a mirror, a snapshot, a token file or a FIT file: they are your health data.

# Contributing

Thanks for looking. Issues and pull requests are welcome; this page says how the code is organised and what a change needs so a review goes quickly.

## Setup

```sh
git clone https://github.com/justmytwospence/garmin-mcp.git && cd garmin-mcp
uv sync --group docs
uv run ruff check         # lint (pyflakes, import order, modern syntax)
uv run pytest -q          # 5 to 9 minutes
uv run mkdocs serve       # the documentation at http://127.0.0.1:8000
```

Python 3.12, [uv](https://docs.astral.sh/uv/) for everything. Dependencies follow a seven-day release cooldown: `[tool.uv] exclude-newer = "7 days"` in `pyproject.toml` resolves only releases at least a week old, `uv.lock` records it, and CI and the Docker build run `uv sync --locked`, which fails on a lockfile resolved under other settings. To take one release early, add it under `[tool.uv.exclude-newer-package]` with that release's upload time as the date, and remove the entry once the release is a week old. The tests never call Garmin: the mirror tests run on scrubbed real payloads in `test/fixtures/payloads.json`, the engine and server tests against a fake Garmin (`test/fake.py`) that serves those fixtures, and FIT decoding on files synthesised by `test/fitgen.py`. Keep it that way: a test that needs a Garmin account cannot run in CI. Fixtures and test data must stay impersonal: synthetic ids, neutral names and coordinates, no real places.

[ruff](https://docs.astral.sh/ruff/) runs in CI with the rules in `pyproject.toml` (`F`, `I`, `UP`); the modules vendored from upstream keep their style and are exempt from the `UP` rules. The code is not auto-formatted: long lines are fine, one statement per line.

`.agents/skills/` holds the DuckDB skills (attaching a database, running queries, the spatial extension) that coding agents used while developing the mirror; they are installed with the `skills` CLI and are not part of the package.

## Layout

| Path | What |
|---|---|
| `src/garmin_mcp/garmin/` | `client.py` (the paced, sequential client and its error classes), `auth.py` (token login, failure classification), `datasets.py` (the registry of every endpoint the mirror keeps), `import_export.py` (Garmin's account export) |
| `src/garmin_mcp/db/` | `tables.py` and `tabledef.py` (the typed schema, declared once), `projections.py` (raw payload to typed rows, and the drift check), `schema.py` (migrations), `raw.py` (landing payloads), `database.py` (the one connection, the write lock, fatal-error handling) |
| `src/garmin_mcp/fit/` | `tables.py` (the FIT schema) and `decode.py` (FIT SDK to tables) |
| `src/garmin_mcp/sync/` | `engine.py` (queue, planner, scheduler, worker) and `fetchers.py` (one Garmin request each) |
| `src/garmin_mcp/strava_sync/` | The optional Strava sync: `config.py` (its settings), `source.py` (strava-mcp's export or snapshot), `reconcile.py` (the pure decisions), `runner.py` (runs them through the paced client and keeps the ledger) |
| `src/garmin_mcp/mirror.py` | Cache-first reads and write-then-refetch for the tools |
| `src/garmin_mcp/tools/` | The tools, grouped by area; `base.py` is the registry and the provenance block; `strava_tools.py` the Strava sync's tools; the vendored upstream modules (header comment on the first line) keep their structure |
| `src/garmin_mcp/http.py`, `snapshot.py`, `cli.py`, `auth_cli.py`, `env.py` | The HTTP surface, the snapshot publisher, the entry points, the settings parser |
| `deploy/` | The compose profiles' files (agentgateway config, Grafana provisioning, the notebook) |
| `docs/`, `scripts/` | The documentation site and the generators of its reference pages (`gen_tools.py`, `gen_schema.py`; `counts.py` keeps the tool and table counts quoted in the prose current) |

## Making a change

- **A new column** on a typed table: add it in `db/tables.py` (or `fit/tables.py`) with its type, comment and unit, and append an `ALTER TABLE` migration in `db/schema.py` that also rebuilds the table's rows from `raw_payload` (`project(tx, type, None)`). Never edit a shipped migration. `test_every_typed_table_matches_its_definition` catches a definition that drifted from the live table, and `test_no_field_garmin_returned_is_left_unmapped` catches a fixture key with no column.
- **A new dataset**: a `Dataset` in `garmin/datasets.py`, a projection in `db/projections.py` with its `Shape`s for the drift check, a fixture, and `test_every_dataset_has_a_projection` keeps you honest.
- **A new tool**: a function in the right `tools/` module registered with `@reg.tool(annotations=...)`; read tools return JSON-able data, writes return a `WriteResult`. Then `uv run python scripts/gen_tools.py` and commit the regenerated `docs/reference/tools.md` (CI checks it). Tool names follow upstream where one exists.
- **Schema or FIT changes**: `uv run python scripts/gen_schema.py` and commit `docs/data/schema.md`. Bump `PARSER_VERSION` in `fit/decode.py` when the decoder's output changes; stored files are decoded again automatically.
- **Docs**: `mkdocs build --strict` must pass; links and anchors are validated. A count quoted in the prose (tools, tables) is wrapped in `<!-- count:name -->N<!-- /count -->` so the generators keep it current.
- **A new setting**: read it through `env.py`, validate it in `cli.validate_settings`, list it in `.env.example` and on the configuration page, and add a changelog line.

Memory matters: the server runs with a 512 MB DuckDB limit in a 1.5 GB container. Projections read payload bodies only through the tokens in `projections.py` (`scoped_raw`, `all_raw`, `last_body`, `history`), which `project()` stages by hash for a scoped run; never join or filter `raw_payload`/`raw_current` for bodies by type and key, which reads the whole body column (`raw.read_current` is the way to read a few), wide projections parse each body once (`_parse_once`), and whole-type scans sample (`live_bodies`). `test/test_memory.py` has the regression tests; `docs/operations.md` the measurements.

## Commits and releases

Conventional commits, one logical change each: `feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `build:`, `ci:`, `chore:`. No emojis. User-visible changes get a line under *Unreleased* in `CHANGELOG.md`.

A release is a `chore: release X.Y.Z` commit that bumps the version in `pyproject.toml` (and `uv.lock`) and turns the *Unreleased* section into `## [X.Y.Z] - date` with its compare link, then a `vX.Y.Z` tag. The `release` workflow then:

1. builds and pushes `ghcr.io/justmytwospence/garmin-mcp:X.Y.Z`, `:X.Y` and `:X` for amd64 and arm64;
2. creates the GitHub Release with that changelog section as its notes;
3. builds the sdist and wheel of `garmin-mirror-mcp` and publishes them to PyPI through [trusted publishing](https://docs.pypi.org/trusted-publishers/). This needs a `pypi` environment on the repository and, on PyPI, a trusted publisher for the project `garmin-mirror-mcp` with owner `justmytwospence`, repository `garmin-mcp`, workflow `release.yml` and environment `pypi`. The job is allowed to fail so a missing setup does not block the image or the release.

The distribution is `garmin-mirror-mcp` (the name `garmin-mcp` belongs to another project on PyPI); the import package and the console scripts keep the `garmin-mcp` name.

## Reporting problems

Use the issue templates. For a sync or login problem, include the `login` and `recent_errors` parts of `garmin_sync_status` and the relevant log lines, with the display name and any identifiers removed. Never attach a mirror, a snapshot, a token file or a FIT file: they are your health data.

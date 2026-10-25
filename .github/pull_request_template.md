## What

<!-- One or two sentences: what changes and why. Link the issue if there is one. -->

## Checklist

- [ ] `uv run ruff check` and `uv run pytest -q` pass
- [ ] Tools or tables changed: `uv run python scripts/gen_tools.py` / `scripts/gen_schema.py` run and the regenerated pages committed
- [ ] Documentation updated where behaviour or configuration changed (`uv run mkdocs build --strict` passes)
- [ ] A line under `[Unreleased]` in `CHANGELOG.md` for user-visible changes
- [ ] No personal data in fixtures, logs or examples (ids, names, places, coordinates)

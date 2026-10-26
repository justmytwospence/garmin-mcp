"""Generate docs/reference/tools.md from the registered tools, and keep the tool counts
quoted in the README and the docs current (see counts.py).

Starts the server against an empty temporary mirror with background sync off (no
Garmin access), lists the tools and writes one section per area with every tool's
description, parameters and kind (read, write, destructive).

    uv run python scripts/gen_tools.py            # write the page
    uv run python scripts/gen_tools.py --check    # exit 1 when the committed page is stale
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from counts import apply as apply_counts  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
STRAVA_MODULE = "garmin_mcp.tools.strava_tools"
OUT = ROOT / "docs" / "reference" / "tools.md"

# Registration module -> section title and blurb, in page order.
SECTIONS: list[tuple[str, str, str]] = [
    ("garmin_mcp.tools.mirror_tools", "The mirror", "SQL, catalog and sync status of the local mirror. No Garmin request, ever."),
    ("garmin_mcp.tools.activities", "Activities", "Listings, one activity's detail, laps, splits, weather, zones, gear and sets, and the edits Garmin Connect allows."),
    ("garmin_mcp.tools.analysis", "Activity analysis", "Analyses computed from the decoded FIT files in the mirror, and file exports."),
    ("garmin_mcp.tools.health", "Health and wellness", "Daily summaries, heart rate, sleep, stress, Body Battery, respiration, pulse ox, hydration, weight, blood pressure, and the ranges and trends computed from them."),
    ("garmin_mcp.tools.training", "Training", "Training status and load, readiness, HRV, VO2 max, acclimation, endurance and hill scores, race predictions, and Garmin-side reports."),
    ("garmin_mcp.tools.workouts", "Workouts and calendar", "Workouts, the training calendar, scheduling, and the workout builders adapted from upstream."),
    ("garmin_mcp.tools.account", "Profile, devices, gear, goals, courses", "The user profile and settings, devices and sensors, gear, heart-rate zones, goals, badges, challenges and courses."),
    ("garmin_mcp.tools.strava_tools", "Strava sync (optional)", "Registered only when `STRAVA_SYNC` is set: Strava activity names onto Garmin, and removal of courses Strava's sync left behind. See [Strava sync](../strava-sync.md)."),
    ("garmin_mcp.tools.nutrition", "Nutrition and women's health", "Pass-through tools vendored from upstream: fetched from Garmin and cached for ten minutes (the mirror does not keep nutrition logs)."),
]


def kind(tool) -> str:
    a = tool.annotations
    if a is None:
        return "read"
    if a.read_only_hint:
        return "read"
    if a.destructive_hint:
        return "destructive"
    return "write"


def params(tool) -> list[tuple[str, str, bool]]:
    schema = tool.input_schema or {}
    required = set(schema.get("required", []))
    out = []
    for name, spec in (schema.get("properties") or {}).items():
        types = spec.get("type")
        if types is None and "anyOf" in spec:
            types = " or ".join(x.get("type", "object") for x in spec["anyOf"])
        elif isinstance(types, list):
            types = " or ".join(types)
        if spec.get("type") == "array" and "items" in spec:
            types = f"array of {spec['items'].get('type', 'object')}"
        if "default" in spec and spec["default"] is not None:
            types = f"{types}, default `{spec['default']}`"
        out.append((name, str(types or "any"), name in required))
    return out


def first_paragraph(text: str) -> str:
    return (text or "").strip().split("\n\n")[0].replace("\n", " ").strip()


def render() -> tuple[str, dict[str, int]]:
    """The page, and the counts the prose quotes."""
    from garmin_mcp.cli import open_runtime
    from garmin_mcp.server import create_server
    from garmin_mcp.tools.base import ToolFilter
    from garmin_mcp.tools.profiles import TOOL_PROFILES

    tmp = Path(tempfile.mkdtemp(prefix="garmin-mcp-tools-"))
    # The optional Strava sync registers its tools only when configured; configure it (never run) so they are listed.
    env = {"GARMIN_DB_PATH": str(tmp / "garmin.duckdb"), "GARMIN_SYNC": "off", "STRAVA_SYNC": "titles,courses", "STRAVA_SNAPSHOT": str(tmp / "strava.duckdb")}
    rt = open_runtime(env, start=False)
    try:
        server = create_server(rt.mirror, ToolFilter())
        tools = asyncio.run(server.list_tools())
        modules = server.registry.modules  # type: ignore[attr-defined]
    finally:
        rt.close()
    by_module: dict[str, list] = {}
    for t in tools:
        by_module.setdefault(modules.get(t.name, "?"), []).append(t)
    counts = {k: sum(1 for t in tools if kind(t) == k) for k in ("read", "write", "destructive")}
    profiles = {name: sorted(names) for name, names in TOOL_PROFILES.items() if names is not None}
    strava_tools = len(by_module.get(STRAVA_MODULE, []))
    mirror_tools = len(by_module.get("garmin_mcp.tools.mirror_tools", []))
    quoted = {
        "tools": len(tools) - strava_tools,  # what a server without the Strava sync registers
        "tools-with-strava": len(tools),
        "tools-upstream": len(tools) - strava_tools - mirror_tools,  # the upstream-named ones
        "tools-mirror": mirror_tools,
        "tools-strava": strava_tools,
        **{f"profile-{name}": len(names) for name, names in profiles.items()},
    }

    lines = [
        "<!-- Generated by scripts/gen_tools.py; do not edit by hand. -->",
        "# Tools reference",
        "",
        f"{len(tools)} tools, generated from the running server: {counts['read']} read-only, {counts['write']} writes, {counts['destructive']} destructive-capable (edits, deletions and overwriting filesystem exports; confirmation only where documented). Tool names, parameters and descriptions follow [Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp), so clients written for it keep working.",
        "",
        "Every result ends with a provenance line: served from the mirror (and when it was fetched), fetched from Garmin just now, or stale and why. Bounded JSON results also carry structured data. See [how sync works](../sync.md#freshness).",
        "",
        "Read `garmin://usage` for resource URI examples, prompt names and API costs, including in clients without template discovery. See [MCP context and API budget](context.md). Local-only tools cost no Garmin requests; cache-first tools may fetch missing/stale data, and ranges, pagination, batches and write refetches multiply that cost. General upstream quota is unknown; check timestamped local status and respect cooldowns. Never blindly replay an ambiguous write. Hints describe behavior, not authorization or spending caps.",
        "",
        "## Profiles",
        "",
        "`GARMIN_TOOL_PROFILE` narrows the set a client sees; `GARMIN_ENABLED_TOOLS` and `GARMIN_DISABLED_TOOLS` add and remove names. The three mirror tools are in every profile.",
        "",
        "| Profile | Tools |",
        "|---|---|",
    ]
    for name, names in profiles.items():
        lines.append(f"| `{name}` | {len(names)}: " + ", ".join(f"`{n}`" for n in names) + " |")
    lines.append("| `all` (default) | every tool below |")
    lines.append("")
    seen_modules = set()
    for module, title, blurb in SECTIONS:
        items = by_module.get(module, [])
        if not items:
            continue
        seen_modules.add(module)
        lines += [f"## {title}", "", blurb, ""]
        for t in items:
            a = t.annotations
            hints = f"readOnly={str(a.read_only_hint).lower()}, destructive={str(a.destructive_hint).lower()}, idempotent={str(a.idempotent_hint).lower()}, openWorld={str(a.open_world_hint).lower()}"
            lines += [f"### `{t.name}`", "", f"*{kind(t)}*. {first_paragraph(t.description)}", "", f"Hints: `{hints}`.", ""]
            ps = params(t)
            if ps:
                lines += ["| Parameter | Type | Required |", "|---|---|---|"]
                lines += [f"| `{n}` | {ty} | {'yes' if r else 'no'} |" for n, ty, r in ps]
                lines.append("")
    leftovers = [m for m in by_module if m not in seen_modules]
    if leftovers:
        raise SystemExit(f"tools registered from modules without a section: {leftovers}")
    return "\n".join(lines).rstrip() + "\n", quoted


def main(argv: list[str]) -> int:
    text, quoted = render()
    if "--check" in argv:
        current = OUT.read_text() if OUT.exists() else ""
        ok = apply_counts(quoted, check=True)
        if current != text:
            print(f"{OUT.relative_to(ROOT)} is stale: run `uv run python scripts/gen_tools.py`", file=sys.stderr)
            return 1
        if not ok:
            print("run `uv run python scripts/gen_tools.py`", file=sys.stderr)
            return 1
        print(f"{OUT.relative_to(ROOT)} and the quoted tool counts are current")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text)
    print(f"wrote {OUT.relative_to(ROOT)}")
    apply_counts(quoted, check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

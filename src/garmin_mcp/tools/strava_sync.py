"""The optional Strava sync's tools: its status, a dry plan, and a manual run. Registered
only when STRAVA_SYNC is set (see the configuration page)."""

from __future__ import annotations

from typing import Any

from .base import DESTRUCTIVE, READ, Registry, ToolFailure, WriteResult

PARTS = ("titles", "courses")


def register(reg: Registry) -> None:
    sync = reg.mirror.engine.strava_sync
    if sync is None:
        return

    @reg.tool(annotations=READ)
    def strava_sync_status() -> Any:
        """Status of the Strava sync: its settings, the last run's result (renames, conflicts, course deletions or why they were held),
        what the ledger links, and its recent actions. No Garmin request."""
        return sync.status()

    @reg.tool(annotations=READ)
    def strava_sync_plan() -> Any:
        """What the Strava sync would do now, without doing it: the Garmin activities it would rename to their Strava names, the
        renames it leaves alone because the Garmin name was edited by hand, and which Strava-made courses it keeps (with the starred
        Strava route each matches and how) or would delete (orphaned: no starred route; or an older duplicate). Re-fetches the
        course list from Garmin first."""
        return sync.plan()

    @reg.tool(annotations=DESTRUCTIVE)
    def strava_sync_apply(part: str, confirm: bool = False) -> Any:
        """Run one part of the Strava sync now, past the per-run cap; for courses also when STRAVA_SYNC_COURSES is dry-run. Deleted
        courses cannot be restored. Call once with confirm=false (the default) for the preview, then with confirm=true.

        Args:
            part: titles (rename Garmin activities to their Strava names) or courses (delete orphaned Strava-made courses).
            confirm: Must be true to act. Default false returns what would be done.
        """
        part = part.strip().lower()
        if part not in PARTS:
            raise ToolFailure(f"part must be one of {', '.join(PARTS)}, got {part!r}")
        if part not in sync.config.parts:
            raise ToolFailure(f"the {part} part is not enabled (STRAVA_SYNC={','.join(sync.config.parts)})")
        if not confirm:
            plan = sync.plan()
            preview = {k: v for k, v in plan.items() if k in ("settings", "problems", "snapshot", part)}
            return {"status": "needs_confirmation", "preview": preview, "message": "Call again with confirm=true to apply this part now (no per-run cap)."}
        return WriteResult(sync.run(priority="interactive", manual={part}))

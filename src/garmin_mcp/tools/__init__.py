"""All tools, registered onto one MCPServer through the Registry."""

from __future__ import annotations

from datetime import timedelta

from . import account, activities, analysis, health, mirror_tools, nutrition, training, workout_templates, workouts
from .base import Registry
from .compat import LegacyClient, UpstreamApp


def register_all(reg: Registry) -> None:
    mirror_tools.register(reg)
    activities.register(reg)
    health.register(reg)
    training.register(reg)
    workouts.register(reg)
    account.register(reg)
    analysis.register(reg)
    nutrition.configure(LegacyClient(reg.mirror, "nutrition", timedelta(minutes=10)))
    nutrition.register_tools(UpstreamApp(reg))
    workout_templates.register_resources(reg.server)


__all__ = ["register_all", "Registry"]

"""Optional Strava sync (STRAVA_SYNC): Strava activity names onto Garmin activities, and
removal of courses Strava's own sync left behind after their route was unstarred."""

from .config import StravaSyncConfig
from .runner import StravaSync

__all__ = ["StravaSync", "StravaSyncConfig"]

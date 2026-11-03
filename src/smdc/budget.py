"""Daily API budget, tracked locally in the ledger.

YouTube counts quota in units per Google Cloud project and resets it at
midnight Pacific time. The ledger only sees calls made by this tool, so the
platform's own "quota exceeded" error stays the final word; the local count is
for estimates, the dashboard gauge and the reset countdown.
"""

import datetime as dt
from zoneinfo import ZoneInfo

from .store import DELETED_SNAPSHOT, Store


class Budget:
    """Records API costs and reports what is left for the day.

    Args:
        store: Database holding the ledger.
        platform: Ledger key, e.g. "youtube".
        daily_units: Daily allowance (10,000 for a standard YouTube key).
        tz: Time zone in which the allowance resets.
    """

    def __init__(self, store: Store, platform: str, daily_units: int,
                 tz: str = "America/Los_Angeles"):
        self.store, self.platform, self.daily_units = store, platform, daily_units
        self.tz = ZoneInfo(tz)
        self.snapshot_id: int | None = None  # set by the pipeline to attribute costs

    def charge(self, endpoint: str, units: int) -> None:
        self.store.charge(self.platform, endpoint, units, self.snapshot_id)

    def _day_start(self) -> dt.datetime:
        local = dt.datetime.now(self.tz)
        return local.replace(hour=0, minute=0, second=0, microsecond=0)

    def used_today(self) -> int:
        start = self._day_start().astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return self.store.units_since(self.platform, start)

    def remaining(self) -> int:
        return max(0, self.daily_units - self.used_today())

    def next_reset(self) -> dt.datetime:
        """Next reset instant, as an aware UTC datetime."""
        return (self._day_start() + dt.timedelta(days=1)).astimezone(dt.timezone.utc)

    def history(self, days: int = 14) -> dict:
        """Units spent per quota day (days start at the reset), and today's breakdown.

        Returns:
            {"days": [{"day": "YYYY-MM-DD", "units": n}, ...] oldest first,
             "endpoints": {endpoint: units today}, "runs": {run name: units today}}.
        """
        start = self._day_start() - dt.timedelta(days=days - 1)
        since = start.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        per_day = {(start + dt.timedelta(days=i)).date().isoformat(): 0 for i in range(days)}
        today = self._day_start().date().isoformat()
        endpoints: dict[str, int] = {}
        runs: dict[str, int] = {}
        for r in self.store.ledger_since(self.platform, since):
            t = dt.datetime.fromisoformat(r["ts"].replace("Z", "+00:00")).astimezone(self.tz)
            day = t.date().isoformat()
            if day in per_day:
                per_day[day] += r["units"]
            if day == today:
                endpoints[r["endpoint"]] = endpoints.get(r["endpoint"], 0) + r["units"]
                name = r["run"] or ("(deleted runs)" if r["snapshot_id"] == DELETED_SNAPSHOT
                                    else "(checks before a run)")
                runs[name] = runs.get(name, 0) + r["units"]
        return {"days": [{"day": d, "units": u} for d, u in per_day.items()],
                "endpoints": endpoints, "runs": runs}

    def status(self) -> dict:
        return {"platform": self.platform, "daily": self.daily_units, "used": self.used_today(),
                "remaining": self.remaining(), "reset": self.next_reset().isoformat()}

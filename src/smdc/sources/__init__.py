"""Registry of platform sources."""

import os

from ..budget import Budget
from .base import BudgetExhausted, Source, Unavailable  # noqa: F401 (re-exported)


def get_source(name: str, budget: Budget) -> Source:
    """Builds the source registered under `name`, with credentials from the environment.

    Raises:
        KeyError: Unknown source name.
        RuntimeError: Missing credentials.
    """
    if name == "youtube":
        from .youtube import YouTubeSource
        key = os.environ.get("YOUTUBE_API_KEY", "").strip()
        if not key:
            raise RuntimeError("YOUTUBE_API_KEY is not set (put it in .env, see .env.example)")
        return YouTubeSource(key, budget)
    raise KeyError(f"unknown source {name!r} (available: {', '.join(SOURCES)})")


SOURCES = ("youtube",)

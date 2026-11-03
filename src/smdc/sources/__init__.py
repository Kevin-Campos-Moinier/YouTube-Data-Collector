"""Registry of platform sources."""

import os

from .. import config, net
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
            raise RuntimeError("no YouTube API key yet: add one in the dashboard (smdc dashboard), "
                               f"or as YOUTUBE_API_KEY= in {config.user_env_path()}")
        return YouTubeSource(key, budget)
    raise KeyError(f"unknown source {name!r} (available: {', '.join(SOURCES)})")


SOURCES = ("youtube",)


def check_key(name: str, key: str, budget: Budget) -> None:
    """Tries a key with one cheap call.

    Raises:
        KeyError: Unknown source name.
        ValueError: The platform refused the key; the message says what to do.
    """
    if name == "youtube":
        from .youtube import YouTubeSource
        return YouTubeSource(key, budget).check_key()
    raise KeyError(f"unknown source {name!r} (available: {', '.join(SOURCES)})")


def classify_targets(name: str, lines: list[str], allow_search: bool = True) -> list[dict]:
    """What each line means, with no network call and no key.

    Returns:
        One dict per line: {"line": number from 1, "kind": video, channel, playlist or search, or
        None for a blank line or a comment; "error": what is wrong, if anything}.
    """
    if name == "youtube":
        from .youtube import classify_target
        out = []
        for n, line in enumerate(lines, 1):
            kind, _ = classify_target(line)
            out.append({"line": n, "kind": None if kind == "invalid" else kind, "error": target_error(line, kind, allow_search)})
        return out
    raise KeyError(f"unknown source {name!r} (available: {', '.join(SOURCES)})")


def target_error(line: str, kind: str | None, allow_search: bool) -> str | None:
    if kind == "invalid":
        return (f"{line.strip()!r} is not a video, channel, playlist or search: use a YouTube link, a video ID, "
                "channel:@name, playlist:ID or search:words")
    if kind == "search" and not allow_search:
        return f"{line.strip()!r} is a search, which costs 100 units per 50 results: tick \"allow search\" to use it"
    return None


def validate_targets(cfg: config.Config) -> None:
    """Refuses a target that cannot work, before any key or quota is needed.

    Raises:
        ValueError: The first such target, and how to write it.
    """
    for row in classify_targets(cfg.source, cfg.targets, cfg.collect.allow_search):
        if row["error"]:
            raise ValueError(row["error"])


def explain_error(e: net.HttpError) -> str:
    """A failed API call in words, with what to do about it."""
    from .youtube import explain_http_error
    return explain_http_error(e)

"""Run configuration: a whole collection described as data.

A run is fully defined by a TOML file (see `config.example.toml`). The parsed
config is frozen into the database when the run is created, so every dataset
can be traced back to the exact parameters that produced it.
"""

import dataclasses
import datetime as dt
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ORDERS = ("relevance", "time", "oldest")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
# Languages lingua chooses among. A short list keeps short comments from being
# attributed to rare languages ("lol ok" -> Zulu); widen it if your corpus needs.
DEFAULT_CANDIDATES = ["en", "fr", "es", "de", "it", "pt", "nl", "ru", "uk", "pl",
                      "tr", "ar", "hi", "id", "ja", "ko", "zh", "vi", "th", "sv"]


@dataclass
class Collect:
    """What to fetch. See `config.example.toml` for the meaning of each field."""

    max_items: int = 100
    order: str = "relevance"
    replies: bool = True
    max_replies: int = 10
    since: str | None = None
    until: str | None = None
    workers: int = 4
    keep_raw: bool = True
    max_videos_per_target: int = 50
    allow_search: bool = False


@dataclass
class Process:
    """How text is cleaned and filtered. Rerunnable without spending quota."""

    languages: list[str] = field(default_factory=lambda: ["en"])
    candidates: list[str] = field(default_factory=lambda: list(DEFAULT_CANDIDATES))
    min_confidence: float = 0.2
    remove_emoji: bool = True
    remove_punct: bool = True
    redact_mentions: bool = True


@dataclass
class Privacy:
    """Whether raw commenter identifiers are kept (off by default)."""

    keep_identifiers: bool = False


@dataclass
class Config:
    """A complete run definition."""

    name: str
    targets: list[str]
    source: str = "youtube"
    daily_units: int = 10_000
    collect: Collect = field(default_factory=Collect)
    process: Process = field(default_factory=Process)
    privacy: Privacy = field(default_factory=Privacy)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def _build(cls, data: dict, where: str):
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown key(s) in [{where}]: {', '.join(sorted(unknown))}")
    return cls(**data)


def from_dict(data: dict) -> Config:
    """Builds and validates a Config from a plain dict (parsed TOML or JSON).

    Raises:
        ValueError: On unknown keys or invalid values.
    """
    data = dict(data)
    sections = {"collect": Collect, "process": Process, "privacy": Privacy}
    parts = {k: _build(cls, data.pop(k, {}) or {}, k) for k, cls in sections.items()}
    cfg = _build(Config, {**data, **parts}, "run")
    validate(cfg)
    return cfg


def load(path: str | Path) -> Config:
    """Reads a TOML run file. Top-level keys live under [run]."""
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    run = raw.pop("run", {})
    return from_dict({**run, **raw})


def validate(cfg: Config) -> None:
    c, p = cfg.collect, cfg.process
    if not NAME_RE.match(cfg.name):
        raise ValueError(f"run name {cfg.name!r}: use letters, digits, '.', '_' or '-'")
    if not [t for t in cfg.targets if t.strip()]:
        raise ValueError("no targets given")
    if c.order not in ORDERS:
        raise ValueError(f"order must be one of {ORDERS}, got {c.order!r}")
    for k in ("max_items", "max_replies", "max_videos_per_target"):
        if getattr(c, k) < 0:
            raise ValueError(f"{k} must be >= 0")
    if not 1 <= c.workers <= 16:
        raise ValueError("workers must be between 1 and 16")
    c.since, c.until = normalize_time(c.since), normalize_time(c.until)
    if not 0 <= p.min_confidence <= 1:
        raise ValueError("min_confidence must be in [0, 1]")
    missing = set(p.languages) - set(p.candidates)
    if missing:
        raise ValueError(f"target language(s) {sorted(missing)} missing from candidates")


def normalize_time(value: str | None) -> str | None:
    """Turns '2024-05-01' or any ISO datetime into 'YYYY-MM-DDTHH:MM:SSZ' (UTC).

    That is the format platforms return, so timestamps compare as strings.
    """
    if not value:
        return None
    t = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def dump_toml(cfg: Config) -> str:
    """Serializes a Config back to TOML (flat sections, scalar and list values)."""

    def val(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(v)
        if isinstance(v, list):
            return "[" + ", ".join(val(x) for x in v) + "]"
        return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'

    d = cfg.to_dict()
    out = ["[run]"]
    out += [f"{k} = {val(v)}" for k, v in d.items() if not isinstance(v, dict)]
    for sec in ("collect", "process", "privacy"):
        out += ["", f"[{sec}]"]
        out += [f"{k} = {val(v)}" for k, v in d[sec].items() if v is not None]
    return "\n".join(out) + "\n"


def user_env_path() -> Path:
    """Where the API key's .env lives by default: the user's config folder, outside any synced or
    shared code folder. `SMDC_ENV` overrides it.

    Returns:
        %APPDATA%\\smdc\\.env on Windows, $XDG_CONFIG_HOME/smdc/.env (default ~/.config/smdc/.env) elsewhere.
    """
    if os.environ.get("SMDC_ENV"):
        return Path(os.environ["SMDC_ENV"]).expanduser()
    if os.name == "nt" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "smdc" / ".env"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "smdc" / ".env"


def load_dotenv(*paths: Path) -> None:
    """Loads KEY=VALUE lines from the first existing .env into os.environ.

    Variables already set in the environment win.
    """
    for p in paths:
        if p.is_file():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            return

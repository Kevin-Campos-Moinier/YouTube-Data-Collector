"""Pseudonymization of commenters.

Platform user IDs (a YouTube channel ID) are public, persistent identifiers:
anyone can open the channel. They are replaced by a keyed hash (HMAC-SHA256)
whose secret stays on the collecting machine. The same user gets the same key
across runs and snapshots, so reply networks and repeat posters stay
analysable, but without the secret the key cannot be linked back to an account.
"""

import hashlib
import hmac
import os
import re
import secrets
from pathlib import Path

MENTION_RE = re.compile(r"(?<![\w@])@@?[\w][\w.\-]*")
# Commenter fields of YouTube comment resources, dropped from stored raw responses.
AUTHOR_FIELDS = {"authorChannelId", "authorDisplayName", "authorChannelUrl", "authorProfileImageUrl"}


def load_secret(data_dir: Path) -> bytes:
    """Returns SOCIALCOLLECT_SECRET, or a per-project secret kept in data_dir/.secret.

    The file is created on first use. Losing it changes every author key;
    sharing it makes the keys reversible for whoever holds the raw data.
    """
    env = os.environ.get("SOCIALCOLLECT_SECRET", "").strip()
    if env:
        return env.encode()
    path = data_dir / ".secret"
    if not path.exists():
        data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # never readable by others
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
    return path.read_text().strip().encode()


def pseudonymize(secret: bytes, platform: str, user_id: str | None) -> str | None:
    """Stable 16-hex-character key for a user, None for missing IDs."""
    if not user_id:
        return None
    return hmac.new(secret, f"{platform}:{user_id}".encode(), hashlib.sha256).hexdigest()[:16]


def scrub_authors(obj):
    """Returns a copy of a raw API response without commenter identifiers (AUTHOR_FIELDS)."""
    if isinstance(obj, dict):
        return {k: scrub_authors(v) for k, v in obj.items() if k not in AUTHOR_FIELDS}
    if isinstance(obj, list):
        return [scrub_authors(v) for v in obj]
    return obj


def redact_mentions(text: str) -> str:
    """Replaces @handles (and YouTube's @@handles) with '@user'."""
    return MENTION_RE.sub("@user", text)

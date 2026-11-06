"""Platform-neutral records exchanged between sources, the pipeline and the store.

A *container* is what comments hang under (a YouTube video, later a Reddit
post). An *item* is one comment or reply. Sources translate their API payloads
into these records; nothing downstream knows which platform produced them.
"""

from dataclasses import dataclass, field


@dataclass
class Container:
    """A video/post whose comments are collected.

    Attributes:
        platform: Source name, e.g. "youtube".
        container_id: Platform identifier (YouTube video ID).
        availability: "ok" or "not_found" (deleted, private or mistyped).
        title: Human-readable title.
        author_id: Uploader's platform ID (a public creator, kept as-is).
        author_name: Uploader's display name.
        published_at: ISO-8601 UTC publication time.
        comment_count: Platform-reported comment count (replies included);
            None when the platform hides it, which on YouTube usually means
            comments are disabled.
        stats: Other platform figures (views, likes, duration in seconds...).
    """

    platform: str
    container_id: str
    availability: str = "ok"
    title: str | None = None
    author_id: str | None = None
    author_name: str | None = None
    published_at: str | None = None
    comment_count: int | None = None
    stats: dict = field(default_factory=dict)


@dataclass
class Item:
    """One comment (depth 0) or reply (depth >= 1).

    Attributes:
        item_id: Platform comment ID.
        container_id: ID of the container it was posted under.
        thread_id: ID of the top-level comment of its thread (itself at depth 0).
        parent_id: ID of the comment it replies to, None at depth 0.
        depth: 0 for top-level comments, 1+ for replies.
        author_id: Commenter's platform ID; pseudonymized before storage.
        author_name: Commenter's display name; dropped before storage by default.
        text: Text exactly as returned by the platform.
        like_count: Likes at collection time.
        reply_count: Platform-reported number of replies (threads only).
        created_at: ISO-8601 UTC posting time.
        updated_at: ISO-8601 UTC last edit time.
        extra: Platform-specific fields that have no neutral column.
    """

    item_id: str
    container_id: str
    thread_id: str
    parent_id: str | None
    depth: int
    author_id: str | None
    author_name: str | None
    text: str
    like_count: int | None
    reply_count: int
    created_at: str | None
    updated_at: str | None
    extra: dict = field(default_factory=dict)


@dataclass
class Page:
    """One unit of progress returned by a source.

    Attributes:
        items: Records obtained from this page.
        raw: Raw API payloads, kept for provenance.
        checkpoint: Opaque JSON-able state to resume after this page, or None
            when the container has no more pages.
    """

    items: list[Item]
    raw: list[dict]
    checkpoint: dict | None = None


@dataclass
class Estimate:
    """Budget units a collection is expected to cost.

    Attributes:
        low: Units certainly spent (comment pages).
        typical: What such a collection usually costs, with its usual reply pages.
        high: Upper bound (adds reply pages that may be needed).
        per_container: container_id -> (typical, high).
    """

    low: int
    typical: int
    high: int
    per_container: dict[str, tuple[int, int]]

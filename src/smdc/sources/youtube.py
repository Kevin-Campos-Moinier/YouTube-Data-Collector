"""YouTube Data API v3 source.

Quota rules this module is built around (costs per call, default 10,000/day):
  videos.list, channels.list, playlistItems.list, commentThreads.list,
  comments.list: 1 unit. search.list has its own limit since June 2026, 100 calls
  a day, and leaves the 10,000 alone.
`commentThreads.list(part=snippet,replies)` returns up to 5 replies per thread
at no extra cost, so `comments.list` is only called for threads with more
replies than that, and only up to the requested cap.
"""

import math
import re
import urllib.parse
from collections.abc import Iterator

from .. import net
from ..budget import Budget
from ..config import Collect
from ..model import Container, Estimate, Item, Page
from ..budget import REFUSED
from .base import BudgetExhausted, Unavailable

API = "https://www.googleapis.com/youtube/v3/"
INLINE_REPLIES = 5
# Threads over 5 replies, measured on 66 varied videos (first 300 threads of each, Nov. 2026): the
# best-ranked comments draw far more replies. A share, not a promise: one video can reach 66%.
REPLY_SHARE, REPLY_SHARE_TOP, TOP_THREADS = 0.01, 0.13, 300
ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded", "dailyLimitExceededUnreg"}
RETRY_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded", "backendError"})
UNAVAILABLE = {"commentsDisabled": "comments_disabled", "videoNotFound": "not_found",
               "forbidden": "forbidden", "processingFailure": "processing_failure",
               "channelNotFound": "not_found", "playlistNotFound": "not_found"}
KEY_REASONS = {"API_KEY_INVALID", "SERVICE_DISABLED", "accessNotConfigured", "API_KEY_SERVICE_BLOCKED",
               "API_KEY_HTTP_REFERRER_BLOCKED", "API_KEY_IP_ADDRESS_BLOCKED", "ipRefererBlocked"}
COST = {"search": 0}  # counted apart: SEARCHES_PER_DAY
SEARCHES_PER_DAY = 100
PROBE_VIDEO = "dQw4w9WgXcQ"  # any public video: the answer does not matter, only that the key is accepted


def parse_video_id(target: str) -> str | None:
    """Extracts a video ID from a bare ID or any common YouTube URL form."""
    t = target.strip()
    if ID_RE.match(t):
        return t
    u = urllib.parse.urlparse(t if "://" in t else "https://" + t)
    host = u.netloc.lower().removeprefix("www.").removeprefix("m.")
    parts = [p for p in u.path.split("/") if p]
    if host == "youtu.be" and parts:
        cand = parts[0]
    elif host.endswith("youtube.com") or host.endswith("youtube-nocookie.com"):
        v = urllib.parse.parse_qs(u.query).get("v")
        if v:
            cand = v[0]
        elif len(parts) >= 2 and parts[0] in ("shorts", "live", "embed", "v"):
            cand = parts[1]
        else:
            return None
    else:
        return None
    return cand if ID_RE.match(cand) else None


def classify_target(raw: str) -> tuple[str | None, str | None]:
    """What one line of targets asks for, with no network call.

    Args:
        raw: A video ID or URL, `channel:@handle`, `channel:UC...`, a channel URL, `playlist:ID`,
            a playlist URL or `search:terms`, with or without a trailing `# comment`.

    Returns:
        (kind, argument). Kind is "video", "channel", "playlist" or "search"; None for a blank line
        or a comment; "invalid" for anything else.
    """
    t = raw.split(" #")[0].strip() if not raw.lstrip().startswith("#") else ""
    if not t:
        return None, None
    try:
        vid = parse_video_id(t)
        kind, _, arg = t.partition(":")
        u = urllib.parse.urlparse(t if "://" in t else "https://" + t)
    except ValueError:  # a malformed URL, such as an unclosed bracket
        return "invalid", None
    if vid:
        return "video", vid
    qs = urllib.parse.parse_qs(u.query)
    path = [p for p in u.path.split("/") if p]
    if kind == "playlist" or "list" in qs:
        ref = arg.strip() if kind == "playlist" else qs["list"][0]
        return ("playlist", ref) if ref else ("invalid", None)
    if kind == "channel":
        return ("channel", arg.strip()) if arg.strip() else ("invalid", None)
    if t.startswith("@") and " " not in t:
        return "channel", t
    if "://" in t or "youtube." in t:
        if path and path[0].startswith("@"):
            return "channel", path[0]
        if len(path) >= 2 and path[0] == "channel":
            return "channel", path[1]
    if kind == "search":
        return ("search", arg.strip()) if arg.strip() else ("invalid", None)
    return "invalid", None


def _reasons(e: net.HttpError) -> set:
    """The error's reason and those in its details, where Google says what is wrong with a key."""
    err = e.body.get("error") if isinstance(e.body.get("error"), dict) else {}
    return {e.reason, *(d.get("reason") for d in err.get("details") or [] if isinstance(d, dict))}


def _key_problem(e: net.HttpError) -> bool:
    return bool(_reasons(e) & KEY_REASONS)


def explain_http_error(e: net.HttpError) -> str:
    """Says in words what a failed call means and what to do about it."""
    if e.status == 0:
        return "Could not reach YouTube: check your internet connection and try again."
    reasons = _reasons(e)
    if "API_KEY_INVALID" in reasons:
        return "YouTube says this API key is not valid: check that you copied all of it, or create a new one."
    if reasons & {"SERVICE_DISABLED", "accessNotConfigured"}:
        return ("The YouTube Data API v3 is not enabled for this key's project: enable it in the Google Cloud "
                "console (APIs & Services), wait a minute and try again.")
    if reasons & {"API_KEY_SERVICE_BLOCKED", "API_KEY_HTTP_REFERRER_BLOCKED", "API_KEY_IP_ADDRESS_BLOCKED",
                  "ipRefererBlocked"}:
        return ("This key's restrictions block the call: on the key in the Google Cloud console, allow the "
                "YouTube Data API v3 and leave Application restrictions on None.")
    if reasons & QUOTA_REASONS:
        return "The daily quota of this key is used up. It resets at midnight, Pacific time."
    if net.daily_limit(e.body):
        return (f"YouTube's limit of {SEARCHES_PER_DAY} searches a day is reached for this key's project. It resets at "
                "midnight, Pacific time. Videos, channels and playlists still work: replace the search: lines.")
    return f"YouTube refused the call (HTTP {e.status} {e.reason}).".replace(" )", ")")


class YouTubeSource:
    """Collects YouTube comments through the official Data API v3.

    Args:
        api_key: A YouTube Data API v3 key.
        budget: Where every call's cost is recorded.
        get: HTTP function (injected in tests).
    """

    name = "youtube"

    def __init__(self, api_key: str, budget: Budget, get=net.get_json):
        self.key, self.budget, self.get = api_key, budget, get

    def check_key(self) -> None:
        """Tries the key on one video (1 unit). A key out of quota is a good key.

        Raises:
            ValueError: YouTube refused the key; the message says what to do.
        """
        self.budget.charge("videos", 1)
        try:
            self.get(API + "videos", {"part": "id", "id": PROBE_VIDEO}, headers={"X-Goog-Api-Key": self.key},
                     retry_reasons=RETRY_REASONS)
        except net.HttpError as e:
            if e.reason not in QUOTA_REASONS:
                raise ValueError(explain_http_error(e)) from None
            self.budget.charge(REFUSED, 0)

    def _call(self, endpoint: str, params: dict) -> dict:
        self.budget.charge(endpoint, COST.get(endpoint, 1))
        try:
            return self.get(API + endpoint, params, headers={"X-Goog-Api-Key": self.key},
                            retry_reasons=RETRY_REASONS)
        except net.HttpError as e:
            if e.reason in QUOTA_REASONS:
                self.budget.charge(REFUSED, 0)
                raise BudgetExhausted(explain_http_error(e)) from None
            if _key_problem(e):  # says "forbidden" too: about the key, not about this video
                raise
            if e.reason in UNAVAILABLE:
                raise Unavailable(UNAVAILABLE[e.reason]) from None
            if e.status == 404:
                raise Unavailable("not_found") from None
            raise

    def _paginate(self, endpoint: str, params: dict, limit: int) -> Iterator[dict]:
        """Yields raw items across pages until `limit` (0 = no limit)."""
        token, n = None, 0
        while True:
            body = self._call(endpoint, {**params, **({"pageToken": token} if token else {})})
            for it in body.get("items", []):
                yield it
                n += 1
                if limit and n >= limit:
                    return
            token = body.get("nextPageToken")
            if not token:
                return

    # -- targets ---------------------------------------------------------------

    def expand(self, targets: list[str], params: Collect) -> list[str]:
        """Accepts video IDs/URLs, `channel:@handle`, `channel:UC...`, channel URLs,
        `playlist:ID`, playlist URLs and `search:terms` (if allowed).

        Lines may carry a trailing `# comment`. Duplicates are dropped, first
        occurrence wins.
        """
        out: dict[str, None] = {}
        limit = params.max_videos_per_target
        for raw in targets:
            kind, arg = classify_target(raw)
            if kind is None:
                continue
            if kind == "video":
                out[arg] = None
                continue
            if kind == "playlist":
                ids = self._playlist(arg, limit)
            elif kind == "channel":
                ids = self._playlist(self._uploads(arg), limit)
            elif kind == "search":
                if not params.allow_search:
                    raise ValueError(f"search: targets are limited to {SEARCHES_PER_DAY} calls a day, 50 results "
                                     "each; set allow_search = true to use them")
                ids = self._search(arg, limit)
            else:
                raise ValueError(f"unrecognized target {raw.strip()!r}")
            out.update(dict.fromkeys(ids))
        return list(out)

    def _uploads(self, ref: str) -> str:
        """Returns the uploads playlist of a channel given '@handle' or 'UC...'."""
        q = {"forHandle": ref} if ref.startswith("@") else {"id": ref}
        items = self._call("channels", {"part": "contentDetails", **q}).get("items", [])
        if not items:
            raise ValueError(f"channel {ref!r} not found")
        return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

    def _playlist(self, playlist_id: str, limit: int) -> list[str]:
        params = {"part": "contentDetails", "playlistId": playlist_id, "maxResults": 50}
        return [it["contentDetails"]["videoId"]
                for it in self._paginate("playlistItems", params, limit)]

    def _search(self, query: str, limit: int) -> list[str]:
        params = {"part": "id", "q": query, "type": "video", "maxResults": 50}
        return [it["id"]["videoId"] for it in self._paginate("search", params, limit)]

    # -- metadata and estimate -------------------------------------------------------

    def resolve(self, ids: list[str]) -> list[Container]:
        found: dict[str, Container] = {}
        for i in range(0, len(ids), 50):
            body = self._call("videos", {"part": "snippet,statistics", "id": ",".join(ids[i:i + 50]),
                                         "maxResults": 50})
            for v in body.get("items", []):
                sn, st = v.get("snippet", {}), v.get("statistics", {})
                cc = st.get("commentCount")
                found[v["id"]] = Container(
                    platform=self.name, container_id=v["id"], title=sn.get("title"),
                    author_id=sn.get("channelId"), author_name=sn.get("channelTitle"),
                    published_at=sn.get("publishedAt"),
                    comment_count=int(cc) if cc is not None else None,
                    stats={k: int(x) for k, x in st.items() if k != "commentCount"})
        return [found.get(i) or Container(self.name, i, availability="not_found") for i in ids]

    def estimate(self, containers: list[Container], params: Collect) -> Estimate:
        """Pages of 100 threads, plus reply pages for threads over 5 replies.

        commentCount includes replies, so thread pages are an upper bound on a
        full crawl; the high end assumes the worst plausible share of threads
        (one in six comments) overflowing the inline replies, and the typical
        cost the measured share (see REPLY_SHARE).
        """
        per, lo_sum, typ_sum, hi_sum = {}, 0, 0, 0
        for c in containers:
            if c.availability != "ok":
                per[c.container_id] = (0, 0)
                continue
            n = c.comment_count
            if n is None:  # hidden count: comments are most likely disabled
                per[c.container_id] = (1, 1)
                lo_sum, typ_sum, hi_sum = lo_sum + 1, typ_sum + 1, hi_sum + 1
                continue
            full = params.order == "oldest" or not params.max_items
            wanted = n if full else min(n, params.max_items)
            lo = max(1, math.ceil(wanted / 100))
            typ = hi = lo
            if params.replies and params.max_replies > INLINE_REPLIES and n:
                sample = n if not params.max_items else min(n, params.max_items)
                hi += min(sample, n // 6) * math.ceil(params.max_replies / 100)
                top = min(sample, TOP_THREADS) if params.order == "relevance" else 0
                typ = min(hi, lo + math.ceil(top * REPLY_SHARE_TOP + (sample - top) * REPLY_SHARE))
            per[c.container_id] = (typ, hi)
            lo_sum, typ_sum, hi_sum = lo_sum + lo, typ_sum + typ, hi_sum + hi
        return Estimate(lo_sum, typ_sum, hi_sum, per)

    # -- comments --------------------------------------------------------------------

    def _item(self, video_id: str, c: dict, thread_id: str, parent_id: str | None,
              reply_count: int = 0) -> Item:
        s = c["snippet"]
        return Item(
            item_id=c["id"], container_id=video_id, thread_id=thread_id, parent_id=parent_id,
            depth=0 if parent_id is None else 1,
            author_id=(s.get("authorChannelId") or {}).get("value"),
            author_name=s.get("authorDisplayName"),
            text=s.get("textOriginal") or s.get("textDisplay") or "",
            like_count=s.get("likeCount"), reply_count=reply_count,
            created_at=s.get("publishedAt"), updated_at=s.get("updatedAt"))

    def threads(self, container: Container, params: Collect,
                checkpoint: dict | None) -> Iterator[Page]:
        vid, p = container.container_id, params
        st = dict(checkpoint or {"token": None, "count": 0})
        capped = p.order != "oldest" and p.max_items > 0
        chrono = p.order != "relevance"  # API returns newest first in these orders
        while True:
            q = {"part": "snippet,replies" if p.replies else "snippet", "videoId": vid,
                 "maxResults": 100, "textFormat": "plainText",
                 "order": "relevance" if p.order == "relevance" else "time"}
            if st["token"]:
                q["pageToken"] = st["token"]
            body = self._call("commentThreads", q)
            items, stop = [], False
            for th in body.get("items", []):
                if capped and st["count"] >= p.max_items:
                    stop = True
                    break
                top = th["snippet"]["topLevelComment"]
                created = top["snippet"].get("publishedAt") or ""
                if p.until and created > p.until:
                    continue
                if p.since and created < p.since:
                    if chrono:
                        stop = True
                        break
                    continue
                tid = th["id"]
                items.append(self._item(vid, top, tid, None, th["snippet"].get("totalReplyCount", 0)))
                inline = (th.get("replies") or {}).get("comments", [])
                items += [self._item(vid, r, tid, tid) for r in inline[:p.max_replies]]
                st["count"] += 1
            st["token"] = body.get("nextPageToken")
            done = stop or not st["token"] or (capped and st["count"] >= p.max_items)
            yield Page(items, [body], None if done else dict(st))
            if done:
                return

    def replies(self, container_id: str, thread_id: str, limit: int) -> Page:
        params = {"part": "snippet", "parentId": thread_id, "maxResults": 100,
                  "textFormat": "plainText"}
        try:
            raw = list(self._paginate("comments", params, limit))
        except Unavailable:  # thread deleted since it was listed
            raw = []
        return Page([self._item(container_id, r, thread_id, thread_id) for r in raw],
                    [{"kind": "youtube#commentListResponse", "parentId": thread_id, "items": raw}])

    def exists(self, item_ids: list[str]) -> set[str]:
        alive = set()
        for i in range(0, len(item_ids), 50):
            body = self._call("comments", {"part": "id", "id": ",".join(item_ids[i:i + 50])})
            alive |= {it["id"] for it in body.get("items", [])}
        return alive

"""YouTube Data API v3 source.

Quota rules this module is built around (costs per call, default 10,000/day):
  videos.list, channels.list, playlistItems.list, commentThreads.list,
  comments.list: 1 unit; search.list: 100 units.
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
from .base import BudgetExhausted, Unavailable

API = "https://www.googleapis.com/youtube/v3/"
INLINE_REPLIES = 5
ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded", "dailyLimitExceededUnreg"}
RETRY_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded", "backendError"})
UNAVAILABLE = {"commentsDisabled": "comments_disabled", "videoNotFound": "not_found",
               "forbidden": "forbidden", "processingFailure": "processing_failure",
               "channelNotFound": "not_found", "playlistNotFound": "not_found"}
COST = {"search": 100}


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

    def _call(self, endpoint: str, params: dict) -> dict:
        self.budget.charge(endpoint, COST.get(endpoint, 1))
        try:
            return self.get(API + endpoint, params, headers={"X-Goog-Api-Key": self.key},
                            retry_reasons=RETRY_REASONS)
        except net.HttpError as e:
            if e.reason in QUOTA_REASONS:
                raise BudgetExhausted(e.reason) from None
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
            t = raw.split(" #")[0].strip() if not raw.lstrip().startswith("#") else ""
            if not t:
                continue
            vid = parse_video_id(t)
            if vid:
                out[vid] = None
                continue
            kind, _, arg = t.partition(":")
            u = urllib.parse.urlparse(t if "://" in t else "https://" + t)
            qs = urllib.parse.parse_qs(u.query)
            path = [p for p in u.path.split("/") if p]
            if kind == "playlist" or "list" in qs:
                ids = self._playlist(arg.strip() if kind == "playlist" else qs["list"][0], limit)
            elif kind == "channel" or t.startswith("@") or (path and (
                    path[0].startswith("@") or path[0] == "channel")):
                if kind == "channel":
                    ref = arg.strip()
                elif t.startswith("@"):
                    ref = t
                else:
                    ref = path[0] if path[0].startswith("@") else path[1]
                ids = self._playlist(self._uploads(ref), limit)
            elif kind == "search":
                if not params.allow_search:
                    raise ValueError("search: targets cost 100 units per 50 results; "
                                     "set allow_search = true to use them")
                ids = self._search(arg.strip(), limit)
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
        (one in six comments) overflowing the inline replies.
        """
        per, lo_sum, hi_sum = {}, 0, 0
        for c in containers:
            if c.availability != "ok":
                per[c.container_id] = (0, 0)
                continue
            n = c.comment_count
            if n is None:  # hidden count: comments are most likely disabled
                per[c.container_id] = (1, 1)
                lo_sum, hi_sum = lo_sum + 1, hi_sum + 1
                continue
            full = params.order == "oldest" or not params.max_items
            wanted = n if full else min(n, params.max_items)
            lo = max(1, math.ceil(wanted / 100))
            hi = lo
            if params.replies and params.max_replies > INLINE_REPLIES and n:
                sample = n if not params.max_items else min(n, params.max_items)
                hi += min(sample, n // 6) * math.ceil(params.max_replies / 100)
            per[c.container_id] = (lo, hi)
            lo_sum, hi_sum = lo_sum + lo, hi_sum + hi
        return Estimate(lo_sum, hi_sum, per)

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

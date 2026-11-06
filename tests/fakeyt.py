"""An in-memory YouTube Data API v3 good enough to exercise the source offline."""

import datetime as dt

from smdc import net

BASE = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
TEXTS = ["This game looks great", "Joel deserved better", "c'est vraiment nul ce jeu",
         "这个游戏看起来很棒", "🔥🔥🔥", "GG", "Qué buena pinta tiene", "lol ok"]


def ts(minutes: int) -> str:
    return (BASE + dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def comment(cid: str, minute: int, text: str, author: str) -> dict:
    return {"id": cid, "snippet": {"textOriginal": text, "textDisplay": text,
                                   "authorDisplayName": "@" + author,
                                   "authorChannelId": {"value": "UC" + author},
                                   "likeCount": minute % 7, "publishedAt": ts(minute),
                                   "updatedAt": ts(minute)}}


def make_video(vid: str, n_threads: int, replies=lambda i: i % 9) -> dict:
    """Threads t0..t{n-1}, t{i} posted at minute i (so higher i = newer)."""
    threads = []
    for i in range(n_threads):
        tid = f"{vid}.t{i}"
        reps = [comment(f"{tid}.r{j}", i + j + 1, f"reply {j} yes you", f"u{(i + j) % 13}")
                for j in range(replies(i))]
        threads.append({"id": tid, "top": comment(tid, i, TEXTS[i % len(TEXTS)], f"u{i % 11}"),
                        "replies": reps})
    return {"threads": threads, "disabled": False}


class FakeYouTube:
    """Callable with net.get_json's signature.

    Attributes:
        videos: video_id -> {"threads": [...], "disabled": bool}.
        calls: list of (endpoint, params) actually served.
        fail_after: raise quotaExceeded once this many calls were served.
    """

    def __init__(self, videos: dict):
        self.videos, self.calls, self.fail_after = videos, [], None

    def __call__(self, url, params, headers=None, retry_reasons=frozenset()):
        assert headers == {"X-Goog-Api-Key": "KEY"} and "key" not in params
        endpoint = url.rsplit("/", 1)[1]
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            raise net.HttpError(403, "quotaExceeded")
        self.calls.append((endpoint, dict(params)))
        return getattr(self, "_" + endpoint)(params)

    def count(self, endpoint: str) -> int:
        return sum(1 for e, _ in self.calls if e == endpoint)

    def _videos(self, p):
        items = []
        for vid in p["id"].split(","):
            v = self.videos.get(vid)
            if v is None:
                continue
            stats = {"viewCount": "1000"}
            if not v["disabled"]:
                # "hidden" comments are counted by YouTube but never returned (held, spam...)
                stats["commentCount"] = str(sum(1 + len(t["replies"]) + t.get("phantom", 0)
                                                for t in v["threads"]) + v.get("hidden", 0))
            items.append({"id": vid, "snippet": {"title": v.get("title", f"Video {vid}"), "channelId": "UCchan",
                                                 "channelTitle": "Chan", "publishedAt": ts(0)},
                          "statistics": stats, "contentDetails": {"duration": "PT12M5S"}})
        return {"items": items}

    def _commentThreads(self, p):
        v = self.videos.get(p["videoId"])
        if v is None:
            raise net.HttpError(404, "videoNotFound")
        if v["disabled"]:
            raise net.HttpError(403, "commentsDisabled")
        threads = list(reversed(v["threads"]))  # newest first
        if p["order"] == "relevance":
            threads.sort(key=lambda t: (-len(t["replies"]), t["id"]))
        start = int(p.get("pageToken", 0))
        page = threads[start:start + int(p["maxResults"])]
        items = []
        for t in page:
            th = {"id": t["id"], "snippet": {"topLevelComment": t["top"],
                                             "totalReplyCount": len(t["replies"]) + t.get("phantom", 0)}}
            if "replies" in p["part"] and t["replies"]:
                th["replies"] = {"comments": t["replies"][:5]}
            items.append(th)
        body = {"items": items}
        if start + len(page) < len(threads):
            body["nextPageToken"] = str(start + len(page))
        return body

    def _all_comments(self):
        for v in self.videos.values():
            for t in v["threads"]:
                yield t["id"], t["top"], t
                for r in t["replies"]:
                    yield r["id"], r, None

    def _comments(self, p):
        if "id" in p:
            ids = set(p["id"].split(","))
            return {"items": [{"id": i} for i, _, _ in self._all_comments() if i in ids]}
        thread = next((t for i, _, t in self._all_comments() if i == p["parentId"] and t), None)
        if thread is None:
            raise net.HttpError(404, "commentNotFound")
        start = int(p.get("pageToken", 0))
        page = thread["replies"][start:start + int(p["maxResults"])]
        body = {"items": page}
        if start + len(page) < len(thread["replies"]):
            body["nextPageToken"] = str(start + len(page))
        return body

    # Target expansion: one channel "@chan" (id UCchan) whose uploads are all videos.
    def _channels(self, p):
        if p.get("forHandle") == "@chan" or p.get("id") == "UCchan":
            return {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UUchan"}}}]}
        return {"items": []}

    def _playlistItems(self, p):
        if p["playlistId"] not in ("UUchan", "PLx"):
            raise net.HttpError(404, "playlistNotFound")
        ids = sorted(self.videos)
        start = int(p.get("pageToken", 0))
        page = ids[start:start + 2]  # tiny pages to exercise pagination
        body = {"items": [{"contentDetails": {"videoId": v}} for v in page]}
        if start + len(page) < len(ids):
            body["nextPageToken"] = str(start + len(page))
        return body

    def _search(self, p):
        return {"items": [{"id": {"videoId": v}} for v in sorted(self.videos)]}

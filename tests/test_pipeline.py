"""End-to-end collection against the in-memory API: counts, quota, resume, refresh."""

import pytest

from fakeyt import FakeYouTube, make_video
from smdc import config, pipeline
from smdc.budget import Budget
from smdc.export import export
from smdc.sources.youtube import YouTubeSource
from smdc.store import Store

SECRET = b"test-secret"


def videos():
    return {"aaaaaaaaaaa": make_video("aaaaaaaaaaa", 250),
            "bbbbbbbbbbb": {**make_video("bbbbbbbbbbb", 3), "disabled": True},
            "ccccccccccc": make_video("ccccccccccc", 40, replies=lambda i: 250 if i == 7 else 0)}


class Env:
    def __init__(self, tmp_path, vids=None):
        self.store = Store(tmp_path / "db.sqlite")
        self.fake = FakeYouTube(vids or videos())
        self.budget = Budget(self.store, "youtube", 10_000)
        self.source = YouTubeSource("KEY", self.budget, get=self.fake)
        self.tmp = tmp_path

    def cfg(self, **collect):
        base = {"max_items": 100, "order": "time", "replies": True, "max_replies": 10, "workers": 2}
        targets = ["https://www.youtube.com/watch?v=aaaaaaaaaaa", "bbbbbbbbbbb",
                   "youtu.be/ccccccccccc", "zzzzzzzzzzz", "aaaaaaaaaaa"]
        return config.from_dict({"name": "t", "targets": targets, "collect": {**base, **collect}})

    def start(self, cfg):
        containers, est = pipeline.plan(self.source, cfg)
        return pipeline.create_run(self.store, cfg, containers), est

    def job(self, run_id):
        return pipeline.Job(self.store, run_id, self.source, self.budget, SECRET)

    def items(self, run_id, seq=-1):
        sid = self.store.snapshots(run_id)[seq]["snapshot_id"]
        return self.store.q("SELECT * FROM items WHERE snapshot_id = ? AND selected = 1 "
                            "ORDER BY item_id", (sid,))


def expected_counts(vid_threads, n, order, max_replies):
    """(comments, replies) the sample should hold for one video."""
    ts = list(reversed(vid_threads))  # newest first
    if order == "oldest":
        ts = vid_threads[:n]
    else:
        ts = ts[:n]
    return len(ts), sum(min(len(t["replies"]), max_replies) for t in ts)


@pytest.mark.parametrize("order", ["time", "oldest", "relevance"])
def test_counts_and_statuses(tmp_path, order):
    env = Env(tmp_path)
    run_id, _ = env.start(env.cfg(order=order))
    assert env.job(run_id).execute() == "done"
    cs = {c["container_id"]: c for c in env.store.containers(env.store.snapshots(run_id)[0]["snapshot_id"])}
    assert list(cs) == ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc", "zzzzzzzzzzz"]  # dedup, order kept
    assert cs["bbbbbbbbbbb"]["error"] == "comments_disabled"
    assert cs["zzzzzzzzzzz"]["phase"] == "unavailable" and cs["zzzzzzzzzzz"]["error"] == "not_found"
    items = env.items(run_id)
    a = [i for i in items if i["container_id"] == "aaaaaaaaaaa"]
    if order != "relevance":
        exp = expected_counts(env.fake.videos["aaaaaaaaaaa"]["threads"], 100, order, 10)
        assert (sum(i["depth"] == 0 for i in a), sum(i["depth"] > 0 for i in a)) == exp
    assert sum(i["depth"] == 0 for i in a) == 100
    c7 = [i for i in items if i["thread_id"] == "ccccccccccc.t7" and i["depth"] > 0]
    assert len(c7) == 10  # capped, although the thread has 250 replies
    assert len({i["item_id"] for i in items}) == len(items)


def test_replies_fetched_only_when_needed(tmp_path):
    env = Env(tmp_path)
    run_id, _ = env.start(env.cfg(order="time", max_replies=10))
    env.job(run_id).execute()
    # Only threads with more than 5 replies need comments.list; the original tool
    # made one call per top-level comment (100 + 40 here).
    threads = env.fake.videos["aaaaaaaaaaa"]["threads"][150:] + env.fake.videos["ccccccccccc"]["threads"]
    assert env.fake.count("comments") == sum(len(t["replies"]) > 5 for t in threads)
    assert env.fake.count("commentThreads") == 1 + 1 + 1  # a: 1 page of 100, b: refused, c: 1 page


def test_oldest_selects_oldest_threads(tmp_path):
    env = Env(tmp_path)
    run_id, _ = env.start(env.cfg(order="oldest", max_items=30))
    env.job(run_id).execute()
    tops = [i["item_id"] for i in env.items(run_id) if i["depth"] == 0 and i["container_id"] == "aaaaaaaaaaa"]
    assert sorted(tops) == sorted(f"aaaaaaaaaaa.t{i}" for i in range(30))


def test_since_stops_paginating(tmp_path):
    env = Env(tmp_path)
    # threads are one minute apart from 2024-01-01T00:00; keep the last 50 of 250
    run_id, _ = env.start(env.cfg(order="time", max_items=0, since="2024-01-01T03:20:00Z"))
    env.job(run_id).execute()
    a = [i for i in env.items(run_id) if i["container_id"] == "aaaaaaaaaaa" and i["depth"] == 0]
    assert len(a) == 50
    pages_a = [p for e, p in env.fake.calls if e == "commentThreads" and p["videoId"] == "aaaaaaaaaaa"]
    assert len(pages_a) == 1


def test_quota_pause_then_resume_matches_uninterrupted(tmp_path):
    ref = Env(tmp_path / "ref")
    run_ref, _ = ref.start(ref.cfg(max_items=0, workers=1))
    ref.job(run_ref).execute()

    env = Env(tmp_path / "cut")
    run_id, _ = env.start(env.cfg(max_items=0, workers=1))
    env.fake.fail_after = len(env.fake.calls) + 2  # dies on the third page
    assert env.job(run_id).execute() == "quota"
    assert env.store.snapshots(run_id)[0]["status"] == "paused"
    partial = len(env.items(run_id))
    assert partial > 0  # nothing collected before the quota error is lost
    env.fake.fail_after = None
    assert env.job(run_id).execute() == "done"
    strip = lambda rows: [(r["item_id"], r["depth"], r["thread_id"]) for r in rows]
    assert strip(env.items(run_id)) == strip(ref.items(run_ref))


def test_pause_by_stop_event(tmp_path):
    env = Env(tmp_path)
    run_id, _ = env.start(env.cfg(max_items=0, workers=1))
    job = env.job(run_id)
    job.stop.set()
    assert job.execute() == "paused"
    assert env.job(run_id).execute() == "done"


def test_refresh_detects_removed_comments(tmp_path):
    env = Env(tmp_path)
    run_id, _ = env.start(env.cfg(order="time", max_items=20, replies=False))
    env.job(run_id).execute()
    vid = env.fake.videos["aaaaaaaaaaa"]
    removed = vid["threads"].pop(245)  # one of the 20 newest
    pipeline.start_refresh(env.store, run_id)
    assert env.job(run_id).execute() == "done"
    s2 = env.store.snapshots(run_id)[1]["snapshot_id"]
    gone = env.store.q("SELECT item_id FROM gone WHERE snapshot_id = ?", (s2,))
    assert [g["item_id"] for g in gone] == [removed["id"]]


def test_export_privacy_and_report(tmp_path):
    env = Env(tmp_path)
    run_id, _ = env.start(env.cfg())
    env.job(run_id).execute()
    out = export(env.store, "t", tmp_path / "exports")
    header = (out / "items.csv").read_text(encoding="utf-8").splitlines()[0].split(",")
    assert "author_key" in header and "author_name" not in header and "text_raw" not in header
    body = (out / "items.csv").read_text(encoding="utf-8")
    assert "@u" not in body and "UCu" not in body
    for f in ("containers.csv", "exclusion_report.json", "run_manifest.json", "codebook.md"):
        assert (out / f).exists()
    rep = env.store.report(env.store.snapshots(run_id)[0]["snapshot_id"])
    for d in rep.values():
        assert d["collected"] == sum(v for k, v in d.items() if k != "collected")
    assert rep["comments"]["other_language"] > 0 and rep["comments"]["empty_after_cleaning"] > 0


def test_raw_responses_keep_no_commenter_identifiers(tmp_path):
    import gzip
    env = Env(tmp_path)
    run_id, _ = env.start(env.cfg())
    env.job(run_id).execute()
    raw = b"".join(gzip.decompress(r["body"]) for r in env.store.q("SELECT body FROM raw"))
    assert b"textOriginal" in raw and b"authorChannelId" not in raw and b"authorDisplayName" not in raw


def test_author_keys_stable_across_snapshots(tmp_path):
    env = Env(tmp_path)
    run_id, _ = env.start(env.cfg(max_items=10, replies=False))
    env.job(run_id).execute()
    pipeline.start_refresh(env.store, run_id)
    env.job(run_id).execute()
    k1 = {i["item_id"]: i["author_key"] for i in env.items(run_id, 0)}
    k2 = {i["item_id"]: i["author_key"] for i in env.items(run_id, 1)}
    assert k1 == k2 and all(k1.values())


def test_target_expansion(tmp_path):
    env = Env(tmp_path)
    col = config.Collect(max_videos_per_target=2)
    all_ids = sorted(env.fake.videos)
    assert env.source.expand(["channel:@chan"], col) == all_ids[:2]
    assert env.source.expand(["https://www.youtube.com/@chan/videos"], col) == all_ids[:2]
    assert env.source.expand(["https://www.youtube.com/channel/UCchan"], col) == all_ids[:2]
    assert env.source.expand(["playlist:PLx  # comment"], config.Collect(max_videos_per_target=0)) == all_ids
    assert env.source.expand(["https://www.youtube.com/playlist?list=PLx"], col) == all_ids[:2]
    with pytest.raises(ValueError, match="allow_search"):
        env.source.expand(["search:tlou trailer"], col)
    before = env.budget.used_today()
    ids = env.source.expand(["search:tlou trailer"], config.Collect(allow_search=True))
    assert ids == all_ids and env.budget.used_today() == before  # searches have their own daily limit
    assert env.budget.history()["searches"] == 1 and "search" not in env.budget.history()["endpoints"]
    with pytest.raises(ValueError, match="unrecognized"):
        env.source.expand(["https://vimeo.com/123"], col)
    with pytest.raises(ValueError, match="not found"):
        env.source.expand(["channel:@nobody"], col)


def test_wait_resumes_after_quota_reset(tmp_path):
    import threading

    env = Env(tmp_path)
    run_id, _ = env.start(env.cfg(max_items=0, workers=1))
    env.fake.fail_after = len(env.fake.calls) + 2
    waits = []

    class InstantReset(threading.Event):
        def wait(self, timeout=None):  # stands in for sleeping until midnight Pacific
            waits.append(timeout)
            env.fake.fail_after = None
            return False

    factory = lambda stop: pipeline.Job(env.store, run_id, env.source, env.budget, SECRET, stop)
    status = pipeline.run_until_done(factory, env.budget, wait=True, say=lambda m: None,
                                     stop=InstantReset())
    assert status == "done" and len(waits) == 1 and waits[0] >= 60


def coverage_env(tmp_path):
    v = make_video("aaaaaaaaaaa", 60)
    v["hidden"] = 37                   # counted by YouTube, never returned
    v["threads"][3]["phantom"] = 2     # 3 real replies, 5 reported
    v["threads"][8]["phantom"] = 4     # 8 real replies, 12 reported
    return Env(tmp_path, {"aaaaaaaaaaa": v, "bbbbbbbbbbb": {**make_video("bbbbbbbbbbb", 2), "disabled": True}})


def test_coverage_full_crawl_accounts_for_every_comment(tmp_path):
    env = coverage_env(tmp_path)
    cfg = env.cfg(max_items=0, max_replies=10)
    cfg.targets = ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    run_id, _ = env.start(cfg)
    env.job(run_id).execute()
    sid = env.store.snapshots(run_id)[0]["snapshot_id"]
    cov = pipeline.coverage(env.store, run_id, sid)
    t = cov["total"]
    assert cov["exhaustive"] and cov["skipped"] == {"comments_disabled": 1}
    assert t["not_sampled"] == 37                      # the API never returned them
    assert t["not_returned"] == 2 + 2 and t["over_reply_cap"] == 2
    assert t["on_platform"] == t["collected"] + t["not_sampled"] + t["over_reply_cap"] + t["not_returned"]
    assert t["collected"] == t["kept"] + t["filtered_out"] and t["filtered_out"] > 0


def test_coverage_capped_run_counts_unsampled_threads(tmp_path):
    env = coverage_env(tmp_path)
    cfg = env.cfg(max_items=10, max_replies=10, order="time")
    cfg.targets = ["aaaaaaaaaaa"]
    run_id, _ = env.start(cfg)
    env.job(run_id).execute()
    cov = pipeline.coverage(env.store, run_id, env.store.snapshots(run_id)[0]["snapshot_id"])
    t, threads = cov["total"], env.fake.videos["aaaaaaaaaaa"]["threads"]
    assert not cov["exhaustive"]
    outside = sum(1 + len(th["replies"]) + th.get("phantom", 0) for th in threads[:50]) + 37
    assert t["not_sampled"] == outside
    assert t["on_platform"] == t["collected"] + t["not_sampled"] + t["over_reply_cap"] + t["not_returned"]


def test_quota_history(tmp_path):
    env = Env(tmp_path)
    run_id, _ = env.start(env.cfg(max_items=10, replies=False))
    env.job(run_id).execute()
    h = env.budget.history(14)
    assert len(h["days"]) == 14 and h["days"][-1]["units"] == env.budget.used_today() > 0
    assert h["runs"]["t"] + h["runs"].get("(checks before a run)", 0) == env.budget.used_today()
    assert set(h["endpoints"]) == {"videos", "commentThreads"}

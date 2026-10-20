"""Dashboard API end to end, with the in-memory YouTube."""

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from fakeyt import FakeYouTube, make_video
from socialcollect import pipeline
from socialcollect.budget import Budget
from socialcollect.dashboard import server
from socialcollect.sources.youtube import YouTubeSource
from socialcollect.store import Store


class Ctx:
    def __init__(self, tmp_path, fake):
        self.data, self.store, self.fake = tmp_path, Store(tmp_path / "db.sqlite"), fake

    def job_factory(self, run_id):
        budget = Budget(self.store, "youtube", 10_000)
        src = YouTubeSource("KEY", budget, get=self.fake)
        return (lambda stop: pipeline.Job(self.store, run_id, src, budget, b"s", stop)), budget


@pytest.fixture
def api(tmp_path, monkeypatch):
    fake = FakeYouTube({"aaaaaaaaaaa": make_video("aaaaaaaaaaa", 120),
                        "bbbbbbbbbbb": make_video("bbbbbbbbbbb", 30)})
    monkeypatch.setattr(server, "get_source", lambda name, b: YouTubeSource("KEY", b, get=fake))
    return server.Api(Ctx(tmp_path, fake))


CFG = {"name": "demo", "targets": ["aaaaaaaaaaa", "bbbbbbbbbbb"],
       "collect": {"max_items": 50, "order": "time", "max_replies": 8}}


def wait(api):
    api.runner.thread.join(timeout=30)
    assert not api.runner.busy()


def test_plan_run_explore_refresh_export(api, tmp_path):
    plan = api.post_plan({"config": CFG})
    assert [c["container_id"] for c in plan["containers"]] == ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    assert plan["estimate"]["low"] == 2
    with pytest.raises(ValueError, match="estimate"):
        api.post_run({"config": {**CFG, "collect": {**CFG["collect"], "max_items": 60}}})
    api.post_run({"config": CFG})
    wait(api)
    assert (tmp_path / "runs" / "demo.toml").exists()
    run = api.get_run({"name": "demo"})
    assert run["snapshot"]["status"] == "done" and run["retention"]
    assert run["report"]["comments"]["collected"] == 80
    ex = api.get_explore({"name": "demo", "kept": "1", "search": "great"})
    assert ex["timeline"] and ex["langs"][0]["lang"] == "en"
    assert all("great" in s["text"].lower() for s in ex["sample"])
    assert api.get_diff({"name": "demo"}) == {"available": False}

    api.post_refresh({"name": "demo"})
    wait(api)
    diff = api.get_diff({"name": "demo"})
    assert diff["available"] and diff["gone"] == [] and diff["matched"] > 0

    api.post_process({"name": "demo", "process": {"languages": []}})
    assert "other_language" not in api.get_run({"name": "demo"})["report"]["comments"]
    out = api.post_export({"name": "demo", "format": "jsonl", "kept": True})
    assert (tmp_path / "exports" / "demo-s2" / "items.jsonl").exists() and out["path"]
    assert api.get_state({})["runs"][0]["name"] == "demo"


def test_http_refuses_cross_origin_posts(api):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(api))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"
    try:
        page = urllib.request.urlopen(url + "/")
        assert b"socialcollect" in page.read() and page.headers["X-Frame-Options"] == "DENY"
        state = json.loads(urllib.request.urlopen(url + "/api/state").read())
        assert state["job"]["busy"] is False
        req = urllib.request.Request(url + "/api/pause", data=b"{}", method="POST",
                                     headers={"Origin": "http://evil.example"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 403
        req = urllib.request.Request(url + "/api/state", headers={"Host": "evil.example:8777"})
        with pytest.raises(urllib.error.HTTPError) as e:  # DNS rebinding
            urllib.request.urlopen(req)
        assert e.value.code == 403
        req = urllib.request.Request(url + "/api/run?name=nope")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 400
    finally:
        srv.shutdown()

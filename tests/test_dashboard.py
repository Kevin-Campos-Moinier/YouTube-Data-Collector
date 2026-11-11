"""Dashboard API end to end, with the in-memory YouTube."""

import csv
import http.client as http_client
import io
import json
import threading
import urllib.error
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer

import pytest

from fakeyt import FakeYouTube, make_video
from smdc import net, pipeline, retention
from smdc.budget import Budget
from smdc.dashboard import server
from smdc.sources.youtube import YouTubeSource
from smdc.store import Store


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
    with pytest.raises(ValueError, match="check the videos"):  # other videos than the ones checked
        api.post_run({"config": {**CFG, "targets": ["aaaaaaaaaaa"]}})
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
        assert b"Social Media Data Collector" in page.read() and page.headers["X-Frame-Options"] == "DENY"
        state = json.loads(urllib.request.urlopen(url + "/api/state").read())
        assert state["job"]["busy"] is False
        theme = urllib.request.urlopen(url + "/ui/theme.css")
        assert theme.headers["Content-Type"].startswith("text/css") and b"--tool" in theme.read()
        assert urllib.request.urlopen(url + "/ui/fonts/newsreader.woff2").headers["Content-Type"] == "font/woff2"
        for path in ("/ui/../server.py", "/ui/%2e%2e/server.py", "/ui/..%2fserver.py", "/ui/", "/ui/nothing.css"):
            with pytest.raises(urllib.error.HTTPError) as e:  # the kit's folder only
                urllib.request.urlopen(url + path)
            assert e.value.code == 404, path
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


def collected(api, name="demo", targets=("aaaaaaaaaaa", "bbbbbbbbbbb")):
    cfg = {**CFG, "name": name, "targets": list(targets)}
    api.post_plan({"config": cfg})
    api.post_run({"config": cfg})
    wait(api)


def db_bytes(tmp_path):
    return b"".join(p.read_bytes() for p in tmp_path.glob("db.sqlite*"))


def test_delete_run_takes_the_data_out_of_the_file(api, tmp_path):
    collected(api)
    collected(api, "other", ["bbbbbbbbbbb"])
    text = "a sentence found in no other run"     # the fake's comments repeat from one run to the next
    with api.store.tx() as db:
        db.execute("""UPDATE items SET text_raw = ? WHERE item_id = (SELECT i.item_id FROM items i
                      JOIN snapshots s USING (snapshot_id) JOIN runs r USING (run_id) WHERE r.name = 'demo' LIMIT 1)""", (text,))
    kept = api.store.q("SELECT COUNT(*) AS n FROM items i JOIN snapshots s USING (snapshot_id) JOIN runs r USING (run_id) "
                       "WHERE r.name = 'other'")[0]["n"]
    assert text.encode() in db_bytes(tmp_path)
    used = api.get_state({})["budget"]["used"]

    assert api.post_delete({"name": "demo"})["snapshots"] == 1

    assert [r["name"] for r in api.get_state({})["runs"]] == ["other"]
    assert text.encode() not in db_bytes(tmp_path)
    for table in ("raw", "containers", "events"):
        assert api.store.q(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"] > 0  # what belongs to the other run stays
    assert api.store.q("SELECT COUNT(*) AS n FROM items")[0]["n"] == kept
    assert api.get_state({})["budget"]["used"] == used  # quota spent stays spent
    assert "(deleted runs)" in api.get_quota({})["runs"]
    with pytest.raises(KeyError):
        api.get_run({"name": "demo"})


def test_delete_run_removes_only_its_own_exports(api, tmp_path):
    collected(api)
    collected(api, "demo-s2", ["bbbbbbbbbbb"])  # its export folder looks like a snapshot of "demo"
    api.post_refresh({"name": "demo"})
    wait(api)
    ours1 = api.post_export({"name": "demo", "seq": 1})["path"]
    ours2 = api.post_export({"name": "demo", "seq": 2})["path"]
    other = api.post_export({"name": "demo-s2"})["path"]
    assert ours2 == other  # the two really do collide on a name: the last export wrote there
    api.post_export({"name": "demo", "seq": 2})
    out = api.post_delete({"name": "demo-s2"})
    assert out["exports"] == []  # that folder now holds snapshot 2 of "demo", not this run's export
    assert api.post_delete({"name": "demo"})["exports"] == sorted(p.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] for p in (ours1, ours2))
    assert not list((tmp_path / "exports").iterdir())


def test_delete_can_keep_the_exports(api, tmp_path):
    collected(api)
    path = api.post_export({"name": "demo"})["path"]
    assert api.post_delete({"name": "demo", "exports": False})["exports"] == []
    assert (tmp_path / "exports" / "demo" / "items.csv").exists() and path


def test_a_kept_export_of_a_deleted_run_survives_a_new_run_of_that_name(api, tmp_path):
    collected(api)
    api.post_export({"name": "demo"})
    api.post_delete({"name": "demo", "exports": False})
    (tmp_path / "exports" / "demo").rename(tmp_path / "exports" / "archive")
    collected(api)
    with api.store.tx() as db:  # a real second run starts later; here both may fall within one second
        db.execute("UPDATE snapshots SET started_at = '2030-01-01T00:00:00Z'")
    assert api.post_delete({"name": "demo"})["exports"] == []
    assert (tmp_path / "exports" / "archive" / "items.csv").exists()


def test_delete_older_snapshots_keeps_the_latest_and_the_page_working(api, tmp_path):
    collected(api)
    api.post_export({"name": "demo"})
    api.post_refresh({"name": "demo"})
    wait(api)
    latest = api.post_export({"name": "demo"})["path"]
    assert api.get_diff({"name": "demo"})["available"]

    out = api.post_delete_older({"name": "demo"})

    assert out["snapshots"] == 1 and out["exports"] == ["demo"]
    assert (tmp_path / "exports" / "demo-s2" / "items.csv").exists() and latest
    run = api.get_run({"name": "demo"})
    assert [s["seq"] for s in run["snapshots"]] == [2] and run["report"]["comments"]["collected"] > 0
    assert api.get_diff({"name": "demo"}) == {"available": False}
    assert api.post_export({"name": "demo"})["path"]  # gone.* still writes with its older snapshot deleted
    assert api.store.q("SELECT COUNT(*) AS n FROM gone")[0]["n"] == 0
    assert api.post_delete_older({"name": "demo"})["snapshots"] == 0  # nothing older left


def test_a_new_run_does_not_inherit_the_units_of_a_deleted_one(api):
    collected(api)
    api.post_delete({"name": "demo"})
    collected(api, "fresh")
    spent = api.get_state({})["runs"][0]["units"]
    collected(api, "second")
    assert api.get_state({})["runs"][1]["units"] == spent  # still only its own


def test_delete_is_refused_while_the_run_collects(api, monkeypatch):
    collected(api)
    monkeypatch.setattr(api.runner, "busy", lambda: True)
    with pytest.raises(RuntimeError, match="pause"):
        api.post_delete({"name": "demo"})
    with pytest.raises(RuntimeError, match="pause"):
        api.post_delete_older({"name": "demo"})
    assert api.store.run("demo")


def test_state_and_run_give_the_date_to_delete_by(api):
    collected(api)
    api.post_refresh({"name": "demo"})
    wait(api)
    run = api.get_run({"name": "demo"})
    assert all(s["delete_by"] for s in run["snapshots"]) and run["retention"] == run["snapshots"][-1]["delete_by"]
    assert api.get_state({})["runs"][0]["delete_by"] == retention.delete_by(
        run["snapshots"][0]["finished_at"] or run["snapshots"][0]["started_at"])


def test_a_bad_target_is_reported_before_the_missing_key(tmp_path, monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    api = server.Api(Ctx(tmp_path, FakeYouTube({})))
    with pytest.raises(ValueError, match="oops"):
        api.post_plan({"config": {"name": "x", "targets": ["oops"]}})
    with pytest.raises(RuntimeError, match="API key"):
        api.post_plan({"config": {"name": "x", "targets": ["aaaaaaaaaaa"]}})
    assert api.post_targets({"lines": ["aaaaaaaaaaa", "oops"]})["lines"][1]["error"]


KEY = "test-key-GOODgoodGOODgoodGOOD0000"


@pytest.fixture
def http(api, tmp_path, monkeypatch):
    monkeypatch.setenv("SMDC_ENV", str(tmp_path / "conf" / ".env"))
    monkeypatch.setenv("YOUTUBE_API_KEY", "")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(api))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def post(url, path, body, **headers):
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    return urllib.request.urlopen(urllib.request.Request(url + path, data=data, method="POST", headers=headers))


def test_key_is_tested_saved_and_never_sent_back(http, api, tmp_path, monkeypatch, capfd):
    seen = []
    monkeypatch.setattr(server, "check_key", lambda name, key, budget: seen.append(key))
    assert api.get_state({})["has_key"] is False
    answer = post(http, "/api/key", {"key": f"  {KEY} "}, Origin=http).read()
    assert seen == [KEY] and json.loads(answer) == {"ok": True}
    state = urllib.request.urlopen(http + "/api/state").read()
    assert json.loads(state)["has_key"] is True and KEY.encode() not in state
    assert f"YOUTUBE_API_KEY={KEY}" in (tmp_path / "conf" / ".env").read_text()
    out, err = capfd.readouterr()
    assert KEY not in out and KEY not in err


@pytest.mark.parametrize("headers", [{}, {"Origin": "http://evil.example"}])
def test_key_needs_a_request_from_the_page(http, tmp_path, monkeypatch, headers):
    monkeypatch.setattr(server, "check_key", lambda *a: pytest.fail("the key must not even be tried"))
    with pytest.raises(urllib.error.HTTPError) as e:
        post(http, "/api/key", {"key": KEY}, **headers)
    assert e.value.code == 403
    assert not (tmp_path / "conf" / ".env").exists()


@pytest.mark.parametrize("bad", ["", "short", "two words here and some more", "A" * 20 + "\nSMDC_SECRET=x"])
def test_key_with_a_bad_format_is_refused_and_not_written(http, tmp_path, monkeypatch, bad):
    monkeypatch.setattr(server, "check_key", lambda *a: pytest.fail("the key must not even be tried"))
    with pytest.raises(urllib.error.HTTPError) as e:
        post(http, "/api/key", {"key": bad}, Origin=http)
    assert e.value.code == 400 and not (tmp_path / "conf" / ".env").exists()


def test_key_refused_by_the_platform_is_not_written(http, tmp_path, monkeypatch):
    def refuse(name, key, budget):
        raise ValueError("YouTube says this API key is not valid")
    monkeypatch.setattr(server, "check_key", refuse)
    with pytest.raises(urllib.error.HTTPError) as e:
        post(http, "/api/key", {"key": KEY}, Origin=http)
    body = e.value.read()
    assert e.value.code == 400 and b"not valid" in body and KEY.encode() not in body
    assert not (tmp_path / "conf" / ".env").exists()


def test_check_key_calls_the_platform_once_and_accepts_a_key_out_of_quota(api):
    budget = api._budget()
    calls = []

    def ok(url, params, headers=None, retry_reasons=frozenset()):
        calls.append((url.rsplit("/", 1)[1], headers))
        return {"items": []}
    YouTubeSource(KEY, budget, get=ok).check_key()
    assert calls == [("videos", {"X-Goog-Api-Key": KEY})] and budget.used_today() == 1

    def out_of_quota(*a, **k):
        raise net.HttpError(403, "quotaExceeded")
    YouTubeSource(KEY, budget, get=out_of_quota).check_key()

    def refused(*a, **k):
        raise net.HttpError(400, "badRequest", {"error": {"details": [{"reason": "API_KEY_INVALID"}]}})
    with pytest.raises(ValueError, match="not valid"):
        YouTubeSource(KEY, budget, get=refused).check_key()


def test_post_bodies_are_capped_and_must_be_json(http):
    # The size is announced but not sent: the server answers before reading a body that large, and a client
    # still writing it could meet a closed connection instead of the answer, depending on timing.
    conn = http_client.HTTPConnection(http.removeprefix("http://"), timeout=10)
    conn.putrequest("POST", "/api/pause")
    conn.putheader("Origin", http)
    conn.putheader("Content-Length", str(server.MAX_BODY + 1))
    conn.endheaders()
    assert conn.getresponse().status == 413
    conn.close()
    with pytest.raises(urllib.error.HTTPError) as e:
        post(http, "/api/pause", b"not json", Origin=http)
    assert e.value.code == 400


def test_changing_a_setting_costs_nothing_but_new_videos_do(api):
    used = lambda: api.get_state({})["budget"]["used"]
    first = api.post_plan({"config": CFG})
    assert first["checked"] is True
    spent = used()
    second = api.post_plan({"config": {**CFG, "collect": {**CFG["collect"], "max_items": 0}}})
    assert second["checked"] is False and used() == spent
    assert second["estimate"]["high"] > first["estimate"]["high"]
    third = api.post_plan({"config": {**CFG, "targets": ["aaaaaaaaaaa"]}})
    assert third["checked"] is True and used() > spent


def test_a_setting_changed_after_the_estimate_can_still_start(api):
    api.post_plan({"config": CFG})
    api.post_run({"config": {**CFG, "collect": {**CFG["collect"], "max_items": 60}}})
    wait(api)
    assert api.get_run({"name": "demo"})["config"]["collect"]["max_items"] == 60


def test_a_check_of_the_videos_expires(api, monkeypatch):
    api.post_plan({"config": CFG})
    now = server.time.time()
    monkeypatch.setattr(server.time, "time", lambda: now + server.RESOLVED_FOR + 1)
    with pytest.raises(ValueError, match="hour"):
        api.post_run({"config": CFG})
    assert api.post_plan({"config": CFG})["checked"] is True


def test_explore_pages_and_orders(api):
    collected(api)
    q = {"name": "demo", "kept": "0"}
    first = api.get_explore(q)
    assert first["total"] > first["page"] == len(first["sample"]) and first["offset"] == 0
    second = api.get_explore({**q, "offset": str(first["page"])})
    assert second["offset"] == first["page"] and second["sample"] != first["sample"]
    assert {s["item_id"] for s in first["sample"]}.isdisjoint(s["item_id"] for s in second["sample"])
    liked = [s["like_count"] for s in api.get_explore({**q, "sort": "liked"})["sample"]]
    assert liked == sorted(liked, reverse=True)
    newest = [s["created_at"] for s in api.get_explore({**q, "sort": "newest"})["sample"]]
    assert newest == sorted(newest, reverse=True)
    assert api.get_explore({**q, "sort": "1; DROP TABLE items"})["total"] == first["total"]  # the order is a fixed list


def test_preview_shows_what_an_export_holds(api):
    collected(api)
    p = api.get_preview({"name": "demo"})
    assert len(p["rows"]) == 5 and p["total"] > 5 and "text" in p["columns"]
    assert "text_raw" not in p["columns"] and "author_name" not in p["columns"]  # pseudonymized, as the export is
    assert api.get_preview({"name": "demo", "kept": "1"})["total"] < p["total"]


def test_exports_are_listed_and_downloaded_and_nothing_else(http, api, tmp_path):
    collected(api)
    path = api.post_export({"name": "demo", "format": "csv", "kept": True})["path"]
    rows = api.get_exports({"name": "demo"})["exports"]
    assert [(r["folder"], r["snapshot"], r["format"], r["kept_only"]) for r in rows] == [("demo", 1, "csv", True)]
    assert rows[0]["rows"] > 0 and rows[0]["path"] == path
    r = urllib.request.urlopen(f"{http}/download/export?name=demo&folder=demo")
    assert r.headers["Content-Type"] == "application/zip" and 'filename="demo.zip"' in r.headers["Content-Disposition"]
    with zipfile.ZipFile(io.BytesIO(r.read())) as z:
        assert "demo/items.csv" in z.namelist() and "demo/codebook.md" in z.namelist()
    (tmp_path / "exports" / "other").mkdir()
    (tmp_path / "secret.txt").write_text("x")
    for bad in ("folder=other", "folder=..", "folder=../..", "folder=", "name=nope&folder=demo"):
        url = f"{http}/download/export?{bad if bad.startswith('name') else 'name=demo&' + bad}"
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(url)
        assert e.value.code == 404, bad
    req = urllib.request.Request(f"{http}/download/export?name=demo&folder=demo", headers={"Host": "evil.example:1"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403


def test_plan_says_where_each_video_comes_from_and_its_figures(api):
    cfg = {**CFG, "targets": ["bbbbbbbbbbb", "search:anything"], "collect": {**CFG["collect"], "allow_search": True}}
    rows = {c["container_id"]: c for c in api.post_plan({"config": cfg})["containers"]}
    assert (rows["bbbbbbbbbbb"]["target"], rows["bbbbbbbbbbb"]["rank"], rows["bbbbbbbbbbb"]["kind"]) == ("bbbbbbbbbbb", 1, "video")
    assert (rows["aaaaaaaaaaa"]["target"], rows["aaaaaaaaaaa"]["rank"], rows["aaaaaaaaaaa"]["kind"]) == ("search:anything", 1, "search")
    assert rows["aaaaaaaaaaa"]["stats"] == {"viewCount": 1000, "durationSeconds": 725}


def test_search_options_need_a_new_check_other_settings_do_not(api):
    cfg = {**CFG, "targets": ["search:anything"], "collect": {**CFG["collect"], "allow_search": True}}
    assert api.post_plan({"config": cfg})["checked"] is True
    assert api.post_plan({"config": {**cfg, "collect": {**cfg["collect"], "max_items": 0}}})["checked"] is False
    again = {**cfg, "collect": {**cfg["collect"], "search_language": "fr"}}
    assert api.post_plan({"config": again})["checked"] is True
    assert api.ctx.fake.calls[-2][1]["relevanceLanguage"] == "fr"


def test_the_check_downloads_as_csv(http, api):
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(f"{http}/download/check")
    assert e.value.code == 404
    api.ctx.fake.videos["aaaaaaaaaaa"]["title"] = "=HYPERLINK(1)"
    cfg = {**CFG, "targets": ["bbbbbbbbbbb", "search:anything"],
           "collect": {**CFG["collect"], "allow_search": True, "search_region": "fr", "search_after": "2022-01-01"}}
    api.post_plan({"config": cfg})
    r = urllib.request.urlopen(f"{http}/download/check")
    assert r.headers["Content-Type"].startswith("text/csv") and "attachment" in r.headers["Content-Disposition"]
    rows = list(csv.DictReader(io.StringIO(r.read().decode())))
    assert list(rows[0]) == list(server.CHECK_COLUMNS)
    by = {x["video_id"]: x for x in rows}
    assert by["bbbbbbbbbbb"]["search_options"] == "" and by["bbbbbbbbbbb"]["rank"] == "1"
    a = by["aaaaaaaaaaa"]
    assert (a["target"], a["rank"], a["views"], a["duration_seconds"], a["title"]) == \
        ("search:anything", "1", "1000", "725", "=HYPERLINK(1)")
    assert a["search_options"] == "order=relevance; region=FR; after=2022-01-01T00:00:00Z; duration=any"
    excel = urllib.request.urlopen(f"{http}/download/check?excel=1").read()
    assert excel.startswith(b"\xef\xbb\xbf") and b";'=HYPERLINK(1);" in excel
    req = urllib.request.Request(f"{http}/download/check", headers={"Host": "evil.example:1"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403

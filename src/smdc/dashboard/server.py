"""Local dashboard: plan, launch, watch, explore and export runs.

A stdlib HTTP server bound to 127.0.0.1 serving one HTML page and a JSON API.
Collections run in a background thread of this process (one at a time, since
they share the daily quota); the page polls for progress.
"""

import dataclasses
import datetime as dt
import json
import os
import re
import shutil
import tempfile
import threading
import time
import traceback
import urllib.parse
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import config, net, pipeline, retention, sources
from ..budget import Budget
from ..export import export, preview
from ..sources import check_key, get_source

PAGE = Path(__file__).with_name("index.html")
# The page's stylesheet, fonts and icons: only these types, and only from this folder.
UI = PAGE.with_name("ui")
UI_TYPES = {".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".woff2": "font/woff2", ".txt": "text/plain; charset=utf-8"}
# A page from another site can point its own domain at 127.0.0.1 (DNS rebinding) and then
# read the API as same-origin; such requests carry that domain in Host, so they are refused.
LOCAL_HOSTS = ("127.0.0.1", "localhost")
EXPLORE_PAGE = 200
EXPLORE_ORDER = {"oldest": "created_at", "newest": "created_at DESC", "liked": "like_count DESC, created_at"}
RESOLVED_FOR = 3600                    # seconds a check of the videos stays good for
MAX_BODY = 1_000_000                   # every request this page sends fits well within this
API_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{20,100}$")


class Runner:
    """The single background collection this dashboard may be running."""

    def __init__(self, ctx):
        self.ctx, self.lock = ctx, threading.Lock()
        self.thread: threading.Thread | None = None
        self.stop = threading.Event()
        self.run_name: str | None = None
        self.waiting = False

    def busy(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def start(self, run_id: int, wait: bool) -> None:
        with self.lock:
            if self.busy():
                raise RuntimeError(f"run {self.run_name!r} is already collecting; pause it first")
            self.stop = threading.Event()
            self.run_name, self.waiting = self.ctx.store.run(run_id)["name"], wait
            self.thread = threading.Thread(target=self._work, args=(run_id, wait), daemon=True)
            self.thread.start()

    def _work(self, run_id: int, wait: bool) -> None:
        store = self.ctx.store
        try:
            factory, budget = self.ctx.job_factory(run_id)
            pipeline.run_until_done(factory, budget, wait, lambda m: store.log(run_id, "info", m),
                                    self.stop)
        except Exception as e:  # surface it in the event log instead of dying silently
            store.log(run_id, "error", sources.explain_error(e) if isinstance(e, net.HttpError)
                      else f"{type(e).__name__}: {e}")
            traceback.print_exc()

    def pause(self) -> None:
        self.stop.set()

    def state(self) -> dict:
        return {"busy": self.busy(), "run": self.run_name, "wait": self.waiting,
                "stopping": self.busy() and self.stop.is_set()}


class Api:
    """JSON endpoints. Each method takes the parsed query/body and returns a dict."""

    def __init__(self, ctx):
        self.ctx, self.store = ctx, ctx.store
        self.runner = Runner(ctx)
        # targets -> (when, resolved containers): checking videos costs quota, changing a setting should not
        self.resolved: dict[str, tuple[float, list]] = {}

    def _budget(self) -> Budget:
        runs = self.store.runs()
        daily = pipeline.run_config(self.store, runs[0]["run_id"]).daily_units if runs else 10_000
        return Budget(self.store, "youtube", daily)

    def _snap(self, name: str, seq: str | None) -> tuple[dict, dict, list]:
        run = self.store.run(name)
        snaps = self.store.snapshots(run["run_id"])
        snap = snaps[-1] if not seq else next(s for s in snaps if s["seq"] == int(seq))
        return run, snap, snaps

    # -- reads -------------------------------------------------------------------

    def get_state(self, q):
        runs = [{**r, "delete_by": retention.delete_by(r["oldest_at"])} for r in self.store.runs()]
        return {"runs": runs, "budget": self._budget().status(),
                "job": self.runner.state(), "defaults": config.from_dict(
                    {"name": "x", "targets": ["x"]}).to_dict(),
                "candidates": config.DEFAULT_CANDIDATES,
                "has_key": bool(os.environ.get("YOUTUBE_API_KEY", "").strip()),
                "key_file": str(config.user_env_path())}

    def get_run(self, q):
        run, snap, snaps = self._snap(q["name"], q.get("seq"))
        sid = snap["snapshot_id"]
        cfg = json.loads(run["config"])
        containers = self.store.containers(sid)
        for c in containers:
            c["replies_left"] = len(self.store.threads_needing_replies(
                sid, c["container_id"], cfg["collect"]["max_replies"])) if c["phase"] == "replies" else 0
        snaps = [{**s, "delete_by": retention.snapshot_delete_by(s)} for s in snaps]
        return {"run": run, "config": cfg, "snapshots": snaps, "snapshot": snap,
                "containers": containers, "report": self.store.report(sid),
                "coverage": pipeline.coverage(self.store, run["run_id"], sid),
                "events": self.store.events(run["run_id"], 40),
                "retention": snaps[-1]["delete_by"]}

    def get_quota(self, q):
        b = self._budget()
        return {"budget": b.status(), **b.history(14),
                "console": "https://console.cloud.google.com/apis/api/youtube.googleapis.com/quotas"}

    def get_explore(self, q):
        _, snap, _ = self._snap(q["name"], q.get("seq"))
        sid = snap["snapshot_id"]
        kept = " AND excluded_reason IS NULL" if q.get("kept") == "1" else ""
        base = f"FROM items WHERE snapshot_id = ? AND selected = 1{kept}"
        timeline = self.store.q(f"""SELECT container_id, substr(created_at, 1, 10) AS day,
                                    COUNT(*) AS n {base} GROUP BY 1, 2 ORDER BY 2""", (sid,))
        langs = self.store.q(f"""SELECT COALESCE(lang, '') AS lang, COUNT(*) AS n {base}
                                 GROUP BY 1 ORDER BY 2 DESC""", (sid,))
        cols = "item_id, container_id, depth, like_count, created_at, text_norm AS text, lang, excluded_reason"
        top = self.store.q(f"SELECT {cols} {base} ORDER BY like_count DESC LIMIT 10", (sid,))
        where, args = [], [sid]
        if q.get("search"):
            where.append("text_norm LIKE ?")
            args.append(f"%{q['search']}%")
        if q.get("container"):
            where.append("container_id = ?")
            args.append(q["container"])
        if q.get("reason"):
            where.append("excluded_reason IS NULL" if q["reason"] == "kept" else "excluded_reason = ?")
            args += [] if q["reason"] == "kept" else [q["reason"]]
        extra = "".join(" AND " + w for w in where)
        order = EXPLORE_ORDER.get(q.get("sort", "oldest"), EXPLORE_ORDER["oldest"])
        offset = max(0, int(q.get("offset") or 0))
        sample = self.store.q(f"SELECT {cols} {base}{extra} ORDER BY {order} LIMIT {EXPLORE_PAGE} OFFSET {offset}", args)
        total = self.store.q(f"SELECT COUNT(*) AS n {base}{extra}", args)[0]["n"]
        return {"timeline": timeline, "langs": langs, "top": top, "sample": sample,
                "total": total, "offset": offset, "page": EXPLORE_PAGE}

    def get_exports(self, q):
        run = self.store.run(q["name"])
        return {"exports": retention.list_exports(self.ctx.data / "exports", run["name"],
                                                  self.store.snapshots(run["run_id"]))}

    def get_preview(self, q):
        return preview(self.store, q["name"], q.get("kept") == "1", int(q["seq"]) if q.get("seq") else None)

    def export_folder(self, name: str, folder: str) -> Path:
        """An export folder of this run, by the name shown in the list.

        Raises:
            KeyError: Not one of this run's exports: nothing else in the data folder can be asked for.
        """
        run = self.store.run(name)
        for path in retention.exports_of(self.ctx.data / "exports", run["name"], self.store.snapshots(run["run_id"])):
            if path.name == folder:
                return path
        raise KeyError(f"no export named {folder!r}")

    def get_diff(self, q):
        run, snap, snaps = self._snap(q["name"], q.get("seq"))
        earlier = [s for s in snaps if s["seq"] < snap["seq"]]  # older snapshots may have been deleted
        if not earlier:
            return {"available": False}
        prev = earlier[-1]
        sid, pid = snap["snapshot_id"], prev["snapshot_id"]
        matched = self.store.q("""SELECT COUNT(*) AS n FROM items n JOIN items p ON p.item_id = n.item_id
                                  AND p.snapshot_id = :p WHERE n.snapshot_id = :s AND n.selected = 1""",
                               {"s": sid, "p": pid})[0]["n"]
        new = self.store.q("""SELECT COUNT(*) AS n FROM items n WHERE n.snapshot_id = :s AND n.selected = 1
                              AND NOT EXISTS (SELECT 1 FROM items p WHERE p.snapshot_id = :p
                              AND p.item_id = n.item_id)""", {"s": sid, "p": pid})[0]["n"]
        missing = len(self.store.missing_since(sid, pid))
        gone = self.store.q("""SELECT g.item_id, g.container_id, p.text_norm AS text, p.like_count,
                               p.created_at FROM gone g JOIN items p ON p.snapshot_id = g.prev_snapshot_id
                               AND p.item_id = g.item_id WHERE g.snapshot_id = ? ORDER BY p.created_at""",
                            (sid,))
        likes = self.store.q("""SELECT n.item_id, n.container_id, p.like_count AS before,
                                n.like_count AS after, n.text_norm AS text FROM items n JOIN items p
                                ON p.item_id = n.item_id AND p.snapshot_id = :p
                                WHERE n.snapshot_id = :s AND n.like_count != p.like_count
                                ORDER BY ABS(n.like_count - p.like_count) DESC LIMIT 15""",
                             {"s": sid, "p": pid})
        return {"available": True, "prev_seq": prev["seq"], "seq": snap["seq"],
                "matched": matched, "new": new, "missing": missing, "gone": gone, "likes": likes}

    # -- actions -------------------------------------------------------------------

    def _cfg(self, body) -> config.Config:
        return config.from_dict(body["config"])

    @staticmethod
    def _targets_key(cfg: config.Config) -> str:
        return json.dumps([cfg.source, cfg.targets, cfg.collect.max_videos_per_target, cfg.collect.allow_search])

    def _resolved(self, cfg: config.Config) -> list | None:
        """The videos of these targets if they were checked recently, so that no quota is spent again."""
        when, containers = self.resolved.get(self._targets_key(cfg), (0.0, None))
        return containers if time.time() - when < RESOLVED_FOR else None

    def post_plan(self, body):
        cfg = self._cfg(body)
        self.store.check_new_name(cfg.name)
        sources.validate_targets(cfg)  # before the key, so that a mistake in a line is what gets reported
        budget = Budget(self.store, cfg.source, cfg.daily_units)
        source = get_source(cfg.source, budget)
        containers = self._resolved(cfg)
        checked = containers is None
        if checked:
            containers = source.resolve(source.expand(cfg.targets, cfg.collect))
            self.resolved[self._targets_key(cfg)] = (time.time(), containers)
        return {"containers": [dataclasses.asdict(c) for c in containers], "checked": checked,
                "estimate": dataclasses.asdict(source.estimate(containers, cfg.collect)), "budget": budget.status()}

    def post_run(self, body):
        cfg = self._cfg(body)
        self.store.check_new_name(cfg.name)
        containers = self._resolved(cfg)
        if containers is None:
            raise ValueError("check the videos first: they changed since the last estimate, or it is over an hour old")
        run_id = pipeline.create_run(self.store, cfg, containers)
        runs_dir = self.ctx.data / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / f"{cfg.name}.toml").write_text(config.dump_toml(cfg), encoding="utf-8")
        self.runner.start(run_id, bool(body.get("wait")))
        return {"name": cfg.name}

    def post_resume(self, body):
        self.runner.start(self.store.run(body["name"])["run_id"], bool(body.get("wait")))
        return {"ok": True}

    def post_refresh(self, body):
        run_id = self.store.run(body["name"])["run_id"]
        if self.runner.busy():
            raise RuntimeError("another collection is running")
        pipeline.start_refresh(self.store, run_id)
        self.runner.start(run_id, bool(body.get("wait")))
        return {"ok": True}

    def post_targets(self, body):
        return {"lines": sources.classify_targets("youtube", body.get("lines", []), bool(body.get("allow_search")))}

    def post_key(self, body):
        """Tries a key, then saves it. The key is never sent back, logged or kept in an error."""
        key = str(body.get("key", "")).strip()
        if not API_KEY_RE.match(key):
            raise ValueError("that does not look like an API key: it is one long word of letters, digits, - and _")
        check_key("youtube", key, self._budget())
        config.save_api_key(key)
        return {"ok": True}

    def _not_collecting(self, name: str) -> None:
        if self.runner.busy() and self.runner.run_name == self.store.run(name)["name"]:
            raise RuntimeError("this run is collecting: pause it first")

    def post_delete(self, body):
        self._not_collecting(body["name"])
        return retention.delete_run(self.store, self.ctx.data, body["name"], bool(body.get("exports", True)))

    def post_delete_older(self, body):
        self._not_collecting(body["name"])
        return retention.delete_older_snapshots(self.store, self.ctx.data, body["name"], bool(body.get("exports", True)))

    def post_pause(self, body):
        self.runner.pause()
        return {"ok": True}

    def post_process(self, body):
        run = self.store.run(body["name"])
        if self.runner.busy() and self.runner.run_name == run["name"]:
            raise RuntimeError("wait until the collection is paused or done")
        cfg = pipeline.run_config(self.store, run["run_id"])
        cfg.process = config.Process(**{**dataclasses.asdict(cfg.process), **body["process"]})
        config.validate(cfg)
        with self.store.tx() as db:
            db.execute("UPDATE runs SET config = ? WHERE run_id = ?",
                       (json.dumps(cfg.to_dict()), run["run_id"]))
        self.store.log(run["run_id"], "info", f"process settings changed: {body['process']}")
        for s in self.store.snapshots(run["run_id"]):
            pipeline.process_snapshot(self.store, s["snapshot_id"], cfg)
        return {"ok": True}

    def post_export(self, body):
        out = export(self.store, body["name"], self.ctx.data / "exports", body.get("format", "csv"),
                     bool(body.get("kept")), bool(body.get("excel")), body.get("seq"))
        return {"path": str(out)}


def make_handler(api: Api):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # keep the terminal for collection logs
            pass

        def _headers(self, code: int, ctype: str, length: int) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            # never inside another site's frame (clickjacking), never content-sniffed
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self._headers(code, ctype, len(body))
            self.end_headers()
            self.wfile.write(body)

        def _download(self, q: dict) -> None:
            """One export as a .zip, written to a temporary file: exports can be large."""
            try:
                folder = api.export_folder(q.get("name", ""), q.get("folder", ""))
            except KeyError:
                return self._send(404, b"no such export", "text/plain")
            with tempfile.TemporaryFile() as tmp:
                with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
                    for f in sorted(folder.rglob("*")):
                        if f.is_file() and not f.is_symlink():
                            z.write(f, Path(folder.name) / f.relative_to(folder))
                size = tmp.tell()
                tmp.seek(0)
                self._headers(200, "application/zip", size)
                self.send_header("Content-Disposition", f'attachment; filename="{folder.name}.zip"')
                self.end_headers()
                shutil.copyfileobj(tmp, self.wfile)

        def _json(self, fn, arg) -> None:
            try:
                out, code = fn(arg), 200
            except (KeyError, ValueError, RuntimeError, StopIteration) as e:
                out, code = {"error": str(e.args[0]) if e.args else type(e).__name__}, 400
            except net.HttpError as e:
                out, code = {"error": sources.explain_error(e)}, 400
            except Exception as e:
                traceback.print_exc()
                out, code = {"error": f"{type(e).__name__}: {e}"}, 500
            self._send(code, json.dumps(out, default=str).encode(), "application/json")

        def _local(self) -> bool:
            host = self.headers.get("Host", "").rsplit(":", 1)[0]
            if host in LOCAL_HOSTS:
                return True
            self._send(403, b"only http://127.0.0.1 is served", "text/plain")
            return False

        def do_GET(self):
            if not self._local():
                return
            u = urllib.parse.urlparse(self.path)
            if u.path == "/":
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if u.path.startswith("/ui/"):
                f = (UI / urllib.parse.unquote(u.path.removeprefix("/ui/"))).resolve()
                if f.is_relative_to(UI.resolve()) and f.is_file() and f.suffix in UI_TYPES:
                    return self._send(200, f.read_bytes(), UI_TYPES[f.suffix])
                return self._send(404, b"not found", "text/plain")
            if u.path == "/download/export":
                return self._download({k: x[0] for k, x in urllib.parse.parse_qs(u.query).items()})
            fn = getattr(api, "get_" + u.path.removeprefix("/api/"), None)
            if not u.path.startswith("/api/") or fn is None:
                return self._send(404, b"not found", "text/plain")
            self._json(fn, {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()})

        def do_POST(self):
            if not self._local():
                return
            fn = getattr(api, "post_" + self.path.removeprefix("/api/"), None)
            if not self.path.startswith("/api/") or fn is None:
                return self._send(404, b"not found", "text/plain")
            origin = self.headers.get("Origin", "").split("//")[-1]
            if origin not in ("", self.headers.get("Host")):
                return self._send(403, b"cross-origin request refused", "text/plain")
            if fn == api.post_key and not origin:  # a page always says where it comes from
                return self._send(403, b"this request must come from the dashboard", "text/plain")
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if not 0 <= n <= MAX_BODY:
                self.close_connection = True
                return self._send(413, b"request too large", "text/plain")
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._send(400, b"not JSON", "text/plain")
            self._json(fn, body)

    return Handler


def serve(ctx, port: int = 8777, open_browser: bool = True) -> None:
    """Serves the dashboard until Ctrl-C (which also pauses a running collection)."""
    api = Api(ctx)
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(api))
    url = f"http://127.0.0.1:{port}"
    print(f"smdc dashboard on {url}  (data: {ctx.data}; Ctrl-C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        api.runner.pause()
        if api.runner.thread:
            print("pausing the running collection after the pages in flight...")
            api.runner.thread.join(timeout=120)
    finally:
        srv.server_close()

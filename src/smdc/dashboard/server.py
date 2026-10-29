"""Local dashboard: plan, launch, watch, explore and export runs.

A stdlib HTTP server bound to 127.0.0.1 serving one HTML page and a JSON API.
Collections run in a background thread of this process (one at a time, since
they share the daily quota); the page polls for progress.
"""

import dataclasses
import datetime as dt
import json
import threading
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import config, pipeline
from ..budget import Budget
from ..export import export
from ..sources import get_source

PAGE = Path(__file__).with_name("index.html")
# A page from another site can point its own domain at 127.0.0.1 (DNS rebinding) and then
# read the API as same-origin; such requests carry that domain in Host, so they are refused.
LOCAL_HOSTS = ("127.0.0.1", "localhost")


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
            store.log(run_id, "error", f"{type(e).__name__}: {e}")
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
        self.plans: dict[str, list] = {}  # plan key -> resolved containers, reused by run

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
        return {"runs": self.store.runs(), "budget": self._budget().status(),
                "job": self.runner.state(), "defaults": config.from_dict(
                    {"name": "x", "targets": ["x"]}).to_dict(),
                "candidates": config.DEFAULT_CANDIDATES}

    def get_run(self, q):
        run, snap, snaps = self._snap(q["name"], q.get("seq"))
        sid = snap["snapshot_id"]
        cfg = json.loads(run["config"])
        containers = self.store.containers(sid)
        for c in containers:
            c["replies_left"] = len(self.store.threads_needing_replies(
                sid, c["container_id"], cfg["collect"]["max_replies"])) if c["phase"] == "replies" else 0
        return {"run": run, "config": cfg, "snapshots": snaps, "snapshot": snap,
                "containers": containers, "report": self.store.report(sid),
                "coverage": pipeline.coverage(self.store, run["run_id"], sid),
                "events": self.store.events(run["run_id"], 40),
                "retention": _plus_days(snaps[-1]["finished_at"] or snaps[-1]["started_at"], 30)}

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
        sample = self.store.q(f"SELECT {cols} {base}{extra} ORDER BY created_at LIMIT 200", args)
        return {"timeline": timeline, "langs": langs, "top": top, "sample": sample}

    def get_diff(self, q):
        run, snap, snaps = self._snap(q["name"], q.get("seq"))
        if snap["seq"] == 1:
            return {"available": False}
        prev = next(s for s in snaps if s["seq"] == snap["seq"] - 1)
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
    def _plan_key(cfg: config.Config) -> str:
        return json.dumps([cfg.source, cfg.targets, dataclasses.asdict(cfg.collect)], sort_keys=True)

    def post_plan(self, body):
        cfg = self._cfg(body)
        self.store.check_new_name(cfg.name)
        budget = Budget(self.store, cfg.source, cfg.daily_units)
        containers, est = pipeline.plan(get_source(cfg.source, budget), cfg)
        self.plans[self._plan_key(cfg)] = containers
        return {"containers": [dataclasses.asdict(c) for c in containers],
                "estimate": dataclasses.asdict(est), "budget": budget.status()}

    def post_run(self, body):
        cfg = self._cfg(body)
        self.store.check_new_name(cfg.name)
        containers = self.plans.get(self._plan_key(cfg))
        if containers is None:
            raise ValueError("estimate the run first (the plan changed since the last estimate)")
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


def _plus_days(ts: str | None, days: int) -> str | None:
    if not ts:
        return None
    t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00")) + dt.timedelta(days=days)
    return t.strftime("%Y-%m-%d")


def make_handler(api: Api):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # keep the terminal for collection logs
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            # never inside another site's frame (clickjacking), never content-sniffed
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, fn, arg) -> None:
            try:
                out, code = fn(arg), 200
            except (KeyError, ValueError, RuntimeError, StopIteration) as e:
                out, code = {"error": str(e.args[0]) if e.args else type(e).__name__}, 400
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
            if self.headers.get("Origin", "").split("//")[-1] not in ("", self.headers.get("Host")):
                return self._send(403, b"cross-origin request refused", "text/plain")
            n = int(self.headers.get("Content-Length") or 0)
            self._json(fn, json.loads(self.rfile.read(n) or b"{}"))

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

"""Orchestration: run a snapshot to completion, pause cleanly, resume later.

Per container, collection goes through phases stored in the database:
  pending -> threads -> select -> replies -> done   (or unavailable)
Each page is committed with its checkpoint, so stopping at any point (quota,
Ctrl-C, dashboard pause, crash) loses at most the page in flight. A failed
container keeps its phase and an `error`; the next resume retries it.
"""

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import __version__, config, text
from .budget import Budget
from .config import Config
from .model import Container, Item
from .privacy import pseudonymize, scrub_authors
from .sources import BudgetExhausted, Source, Unavailable
from .store import Store, now

log = logging.getLogger(__name__)


def run_config(store: Store, run_id: int) -> Config:
    return config.from_dict(json.loads(store.run(run_id)["config"]))


def create_run(store: Store, cfg: Config, containers: list[Container] | None = None) -> int:
    """Registers a run and its first snapshot; `containers` skips re-resolving.

    Returns:
        The new run_id.
    """
    run_id = store.create_run(cfg, __version__)
    sid = store.new_snapshot(run_id)
    if containers is not None:
        store.add_containers(sid, containers)
    return run_id


def start_refresh(store: Store, run_id: int) -> int:
    """Opens a new snapshot re-collecting the same containers.

    Raises:
        RuntimeError: If the latest snapshot is not finished yet.
    """
    snaps = store.snapshots(run_id)
    if snaps and snaps[-1]["status"] != "done":
        raise RuntimeError(f"snapshot {snaps[-1]['seq']} is {snaps[-1]['status']}; resume it first")
    return store.new_snapshot(run_id)


def plan(source: Source, cfg: Config) -> tuple[list[Container], object]:
    """Expands targets and resolves containers (costs a few units), then estimates."""
    containers = source.resolve(source.expand(cfg.targets, cfg.collect))
    return containers, source.estimate(containers, cfg.collect)


class Job:
    """Executes the open snapshot of a run.

    Args:
        store: Project database.
        run_id: Run to execute.
        source: Platform adapter.
        budget: Quota tracker shared with the source.
        secret: Pseudonymization key.
        stop: Set it to pause at the next page boundary.
    """

    def __init__(self, store: Store, run_id: int, source: Source, budget: Budget,
                 secret: bytes, stop: threading.Event | None = None):
        self.store, self.run_id, self.source, self.budget = store, run_id, source, budget
        self.secret, self.stop = secret, stop or threading.Event()
        self.cfg = run_config(store, run_id)
        self.quota_hit = False

    def say(self, level: str, msg: str) -> None:
        getattr(log, level)(msg)
        self.store.log(self.run_id, level, msg)

    def execute(self) -> str:
        """Runs until done, paused or out of quota.

        Returns:
            "done", "incomplete" (some containers failed; resume retries them),
            "paused" (stop was set) or "quota" (budget exhausted).
        """
        snap = self.store.snapshots(self.run_id)[-1]
        sid = snap["snapshot_id"]
        if snap["status"] == "done":
            return "done"
        self.budget.snapshot_id = sid
        self.store.set_snapshot(sid, status="running", message=None)
        try:
            if not self.store.containers(sid):
                self._resolve(snap)
            pending = [c for c in self.store.containers(sid)
                       if c["phase"] not in ("done", "unavailable")]
            self.say("info", f"snapshot {snap['seq']}: {len(pending)} container(s) to collect")
            with ThreadPoolExecutor(self.cfg.collect.workers) as ex:
                futs = [ex.submit(self._container, sid, c) for c in pending]
                try:
                    for f in as_completed(futs):
                        try:
                            f.result()
                        except BudgetExhausted:  # the other workers stop at their next page
                            self.quota_hit = True
                except KeyboardInterrupt:
                    self.say("warning", "interrupted; finishing the pages in flight")
                    self.stop.set()
            if snap["seq"] > 1 and not self.halted() and not self._failed(sid):
                self._verify_gone(sid, snap["run_id"])
        except BudgetExhausted:
            self.quota_hit = True
        return self._finish(sid)

    def halted(self) -> bool:
        return self.quota_hit or self.stop.is_set()

    def _failed(self, sid: int) -> list[dict]:
        return [c for c in self.store.containers(sid) if c["phase"] not in ("done", "unavailable")]

    def _finish(self, sid: int) -> str:
        if self.quota_hit:
            reset = self.budget.next_reset().strftime("%Y-%m-%d %H:%M UTC")
            msg = f"quota exhausted; resumable after the reset at {reset}"
            status, ret = "paused", "quota"
        elif self.stop.is_set():
            msg, status, ret = "paused", "paused", "paused"
        else:
            failed = self._failed(sid)
            if failed:
                msg = f"{len(failed)} container(s) failed; resume to retry"
                status = ret = "incomplete"
            else:
                msg, status, ret = None, "done", "done"
                self.store.set_snapshot(sid, finished_at=now())
        self.store.set_snapshot(sid, status=status, message=msg)
        self.say("warning" if ret != "done" else "info", f"snapshot {status}" + (f": {msg}" if msg else ""))
        return ret

    def _resolve(self, snap: dict) -> None:
        if snap["seq"] > 1:  # a refresh follows the containers of the first snapshot
            first = self.store.snapshots(self.run_id)[0]["snapshot_id"]
            ids = [c["container_id"] for c in self.store.containers(first)]
        else:
            ids = self.source.expand(self.cfg.targets, self.cfg.collect)
        self.store.add_containers(snap["snapshot_id"], self.source.resolve(ids))

    def _rows(self, items: list[Item]) -> list[dict]:
        """Store rows for a page: pseudonymized, and processed so they are usable at once."""
        ts = now()
        rows = [self._row(it, ts) for it in items]
        cols = ("text_norm", "text_clean", "lang", "lang_conf", "excluded_reason")
        for r, res in zip(rows, text.process([it.text for it in items], self.cfg.process)):
            r.update(zip(cols, res))
        return rows

    def _row(self, it: Item, ts: str) -> dict:
        keep = self.cfg.privacy.keep_identifiers
        return {"item_id": it.item_id, "container_id": it.container_id, "thread_id": it.thread_id,
                "parent_id": it.parent_id, "depth": it.depth,
                "author_key": pseudonymize(self.secret, self.source.name, it.author_id),
                "author_id": it.author_id if keep else None,
                "author_name": it.author_name if keep else None,
                "text_raw": it.text, "like_count": it.like_count, "reply_count": it.reply_count,
                "created_at": it.created_at, "updated_at": it.updated_at, "collected_at": ts,
                "selected": 1, "extra": json.dumps(it.extra) if it.extra else None}

    def _raw(self, raw: list[dict]) -> list[dict] | None:
        """Raw responses to store: none without keep_raw, scrubbed without keep_identifiers."""
        if not self.cfg.collect.keep_raw:
            return None
        return raw if self.cfg.privacy.keep_identifiers else scrub_authors(raw)

    def _container(self, sid: int, c: dict) -> None:
        cid, col = c["container_id"], self.cfg.collect
        phase = c["phase"]
        if self.halted():
            return
        self.store.set_container(sid, cid, error=None)
        try:
            if phase in ("pending", "threads"):
                phase = "threads"
                self.store.set_container(sid, cid, phase=phase)
                cont = Container(c["platform"], cid, comment_count=c["comment_count"])
                ckpt = json.loads(c["checkpoint"]) if c["checkpoint"] else None
                for page in self.source.threads(cont, col, ckpt):
                    self.store.save_page(sid, cid, self._rows(page.items),
                                         self._raw(page.raw), page.checkpoint)
                    if page.checkpoint is None:
                        phase = "select"  # set by save_page in the same transaction
                    elif self.halted():
                        return
            if phase == "select":
                if col.order == "oldest":
                    self.store.select_oldest(sid, cid, col.max_items)
                phase = "replies"
                self.store.set_container(sid, cid, phase=phase)
            if phase == "replies" and col.replies:
                for t in self.store.threads_needing_replies(sid, cid, col.max_replies):
                    if self.halted():
                        return
                    page = self.source.replies(cid, t["item_id"], min(t["reply_count"], col.max_replies))
                    self.store.save_replies(sid, cid, t["item_id"], self._rows(page.items),
                                            self._raw(page.raw))
            self.store.set_container(sid, cid, phase="done")
            done = self.store.q("SELECT threads, replies FROM containers WHERE snapshot_id = ? "
                                "AND container_id = ?", (sid, cid))[0]
            self.say("info", f"{cid}: done ({done['threads']} comments, {done['replies']} replies)")
        except Unavailable as e:
            self.store.set_container(sid, cid, phase="unavailable", error=e.reason)
            self.say("warning", f"{cid}: skipped ({e.reason})")
        except BudgetExhausted:
            raise
        except Exception as e:  # one bad container must not sink the run
            self.store.set_container(sid, cid, error=f"{type(e).__name__}: {e}")
            self.say("error", f"{cid}: {type(e).__name__}: {e}")

    def _verify_gone(self, sid: int, run_id: int) -> None:
        """Checks by ID which previously sampled items the refresh did not see."""
        snaps = [s for s in self.store.snapshots(run_id) if s["snapshot_id"] < sid and s["status"] == "done"]
        if not snaps:
            return
        prev = snaps[-1]["snapshot_id"]
        missing = self.store.missing_since(sid, prev)
        if not missing:
            return
        alive = self.source.exists([m["item_id"] for m in missing])
        gone = [m for m in missing if m["item_id"] not in alive]
        self.store.mark_gone(sid, prev, gone)
        self.say("info", f"refresh: {len(missing)} earlier comments not re-sampled, "
                         f"{len(gone)} of them no longer public")


def coverage(store: Store, run_id: int, snapshot_id: int) -> dict:
    """Coverage of a snapshot under its run's settings (see Store.coverage)."""
    c = run_config(store, run_id).collect
    return store.coverage(snapshot_id, c.max_replies if c.replies else 0,
                          exhaustive=not c.max_items and not c.since and not c.until)


def process_snapshot(store: Store, snapshot_id: int, cfg: Config,
                     container_id: str | None = None) -> None:
    """(Re)computes normalized/clean text, language and exclusion reason. No API calls."""
    rows = store.texts(snapshot_id, container_id)
    if not rows:
        return
    results = text.process([r["text_raw"] for r in rows], cfg.process)
    store.save_processed(snapshot_id, [(*res, r["item_id"]) for res, r in zip(results, rows)])


def run_until_done(job_factory, budget: Budget, wait: bool, say=print,
                   stop: threading.Event | None = None) -> str:
    """Executes a job; with `wait`, sleeps through quota resets until done.

    Args:
        job_factory: Callable taking the stop event and returning a fresh Job.
        budget: Used for the reset time.
        wait: Keep going across quota resets.
        say: Progress printer.
        stop: Pauses the job, and also interrupts the wait.
    """
    stop = stop or threading.Event()
    while True:
        status = job_factory(stop).execute()
        if status != "quota" or not wait:
            return status
        reset = budget.next_reset()
        say(f"quota exhausted; waiting until {reset:%Y-%m-%d %H:%M} UTC (+2 min), then resuming")
        if stop.wait(max(60.0, reset.timestamp() - time.time() + 120)):
            return "paused"

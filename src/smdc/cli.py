"""Collect social media comments for research through the platforms' official APIs.

    smdc init                       # save your YouTube API key
    smdc doctor [--online]          # check this computer's setup
    smdc estimate run.toml          # resolve targets, show cost; costs ~1 unit/50 videos
    smdc run run.toml [--wait]      # create the run and collect
    smdc resume NAME [--wait]       # continue a paused/incomplete run
    smdc refresh NAME [--wait]      # new snapshot of the same videos
    smdc process NAME [--config f]  # redo text processing (no API calls)
    smdc export NAME [--format csv|jsonl|parquet] [--kept] [--excel]
    smdc status [NAME]
    smdc delete NAME [--older-snapshots] [--keep-exports] [--yes]
    smdc dashboard [--port 8777]

Global option --data DIR (default $SMDC_DATA or ~/smdc-data) selects the
folder holding the database, the pseudonymization secret, run files and exports.
"""

import argparse
import getpass
import importlib.util
import json
import logging
import os
import stat
import sys
from pathlib import Path

from . import __version__, config, net, pipeline, retention
from .budget import Budget
from .export import export
from .privacy import load_secret
from .sources import check_key, explain_error, get_source, validate_targets
from .store import Store

# Outside the code folder on purpose: a database in a synced folder (OneDrive, Dropbox, iCloud)
# that two computers write to gets corrupted.
DEFAULT_DATA = Path.home() / "smdc-data"
EXAMPLES = """examples:
  smdc init                        save your YouTube API key (or do it in the dashboard)
  smdc dashboard                   the local web page: plan, launch, watch, explore, export
  smdc estimate my-run.toml        check the videos and the cost, for about 1 unit per 50 videos
  smdc run my-run.toml --wait      collect, across quota resets if need be
  smdc export my-run --kept        write the comments that passed the language filter
"""


class Ctx:
    """Shared handles for one command: database, secret, data folder."""

    def __init__(self, data_dir: Path):
        self.data = data_dir.resolve()
        self.data.mkdir(mode=0o700, parents=True, exist_ok=True)  # personal data: owner only
        # the user's config folder first; a .env in the working folder still works (versions up to 2.0.0)
        config.load_dotenv(config.user_env_path(), Path.cwd() / ".env", self.data.parent / ".env")
        self.store = Store(self.data / "smdc.sqlite")

    def secret(self) -> bytes:
        return load_secret(self.data)

    def budget(self, cfg: config.Config) -> Budget:
        return Budget(self.store, cfg.source, cfg.daily_units)

    def job_factory(self, run_id: int):
        cfg = pipeline.run_config(self.store, run_id)
        budget = self.budget(cfg)
        source = get_source(cfg.source, budget)
        secret = self.secret()
        return (lambda stop: pipeline.Job(self.store, run_id, source, budget, secret, stop)), budget


def print_plan(containers, est, budget: Budget) -> None:
    print(f"\n{'video':<13} {'comments':>9} {'units':>9}  title")
    for c in containers:
        lo, hi = est.per_container[c.container_id]
        cc = "hidden" if c.comment_count is None else f"{c.comment_count:,}"
        units = f"{lo}" if lo == hi else f"{lo}-{hi}"
        note = "" if c.availability == "ok" else f"[{c.availability}] "
        print(f"{c.container_id:<13} {cc:>9} {units:>9}  {note}{(c.title or '')[:60]}")
    b = budget.status()
    print(f"\nestimated cost: {est.low}-{est.high} units; "
          f"remaining today: {b['remaining']:,}/{b['daily']:,} (resets {b['reset'][:16]} UTC)")
    hidden = sum(1 for c in containers if c.availability == "ok" and c.comment_count is None)
    if hidden:
        print(f"{hidden} video(s) hide their comment count: comments are probably disabled "
              "(1 unit each to confirm).")


def cmd_estimate(ctx: Ctx, a) -> int:
    cfg = config.load(a.config)
    validate_targets(cfg)
    budget = ctx.budget(cfg)
    containers, est = pipeline.plan(get_source(cfg.source, budget), cfg)
    print_plan(containers, est, budget)
    print(f"\nnext: smdc run {a.config}")
    return 0


def cmd_run(ctx: Ctx, a) -> int:
    cfg = config.load(a.config)
    ctx.store.check_new_name(cfg.name)
    validate_targets(cfg)
    budget = ctx.budget(cfg)
    containers, est = pipeline.plan(get_source(cfg.source, budget), cfg)
    print_plan(containers, est, budget)
    if est.low > budget.remaining() and not a.force:
        print("\nnot enough quota left today for even the low estimate; "
              "use --force to start anyway (the run pauses and resumes with `resume`).")
        return 2
    run_id = pipeline.create_run(ctx.store, cfg, containers)
    (ctx.data / "runs").mkdir(parents=True, exist_ok=True)
    (ctx.data / "runs" / f"{cfg.name}.toml").write_text(config.dump_toml(cfg), encoding="utf-8")
    return _execute(ctx, run_id, a.wait)


def _execute(ctx: Ctx, run_id: int, wait: bool) -> int:
    factory, budget = ctx.job_factory(run_id)
    status = pipeline.run_until_done(factory, budget, wait)
    name = ctx.store.run(run_id)["name"]
    print_status(ctx, name)
    print(f"\nnext: smdc export {name}" if status == "done" else f"\nnext: smdc resume {name} --wait")
    return 0 if status == "done" else 1


def cmd_resume(ctx: Ctx, a) -> int:
    return _execute(ctx, ctx.store.run(a.run)["run_id"], a.wait)


def cmd_refresh(ctx: Ctx, a) -> int:
    run_id = ctx.store.run(a.run)["run_id"]
    pipeline.start_refresh(ctx.store, run_id)
    return _execute(ctx, run_id, a.wait)


def cmd_process(ctx: Ctx, a) -> int:
    run = ctx.store.run(a.run)
    cfg = pipeline.run_config(ctx.store, run["run_id"])
    if a.config:
        cfg.process = config.load(a.config).process
        config.validate(cfg)
        with ctx.store.tx() as db:
            db.execute("UPDATE runs SET config = ? WHERE run_id = ?",
                       (json.dumps(cfg.to_dict()), run["run_id"]))
        ctx.store.log(run["run_id"], "info", f"process settings changed from {a.config}")
    for s in ctx.store.snapshots(run["run_id"]):
        pipeline.process_snapshot(ctx.store, s["snapshot_id"], cfg)
    print(json.dumps(ctx.store.report(ctx.store.snapshots(run["run_id"])[-1]["snapshot_id"]), indent=2))
    return 0


def cmd_export(ctx: Ctx, a) -> int:
    out = export(ctx.store, a.run, Path(a.out) if a.out else ctx.data / "exports", a.format,
                 a.kept, a.excel, a.snapshot)
    print(f"exported to {out}")
    snaps = ctx.store.snapshots(ctx.store.run(a.run)["run_id"])
    print(f"next: open codebook.md there. YouTube's 30-day rule: refresh or delete this data by {retention.snapshot_delete_by(snaps[0])}.")
    return 0


def print_status(ctx: Ctx, ref: str | None) -> None:
    if not ref:
        for r in ctx.store.runs():
            print(f"{r['name']:<30} snapshot {r['seq']} {r['status']:<11} {r['units']:>6} units"
                  f"  delete by {retention.delete_by(r['oldest_at'])}  {r['message'] or ''}")
        return
    run = ctx.store.run(ref)
    for s in ctx.store.snapshots(run["run_id"]):
        print(f"snapshot {s['seq']}: {s['status']} ({s['units']} units), delete by "
              f"{retention.snapshot_delete_by(s)} {s['message'] or ''}")
    snap = ctx.store.snapshots(run["run_id"])[-1]
    for c in ctx.store.containers(snap["snapshot_id"]):
        print(f"  {c['container_id']}  {c['phase']:<11} {c['threads']:>6} comments "
              f"{c['replies']:>6} replies  {c['error'] or ''}")
    rep = ctx.store.report(snap["snapshot_id"])
    for kind, d in rep.items():
        print(f"  {kind}: " + ", ".join(f"{k}={v}" for k, v in sorted(d.items())))
    cov = pipeline.coverage(ctx.store, run["run_id"], snap["snapshot_id"])
    t = cov["total"]
    if t["videos"]:
        pct = lambda n: f"{n:,} ({100 * n / t['on_platform']:.0f}%)" if t["on_platform"] else f"{n:,}"
        gap = "never returned by the API" if cov["exhaustive"] else "outside the sample (cap/window) or not returned"
        print(f"  coverage over {t['videos']} finished video(s): {t['on_platform']:,} comments on the platform")
        print(f"    collected {pct(t['collected'])}, of which kept {pct(t['kept'])}")
        print(f"    {gap}: {pct(t['not_sampled'])}; replies over the cap: {pct(t['over_reply_cap'])}; "
              f"replies reported but not returned: {pct(t['not_returned'])}")
    if cov["skipped"]:
        print("  skipped videos: " + ", ".join(f"{k}={v}" for k, v in cov["skipped"].items()))


def cmd_status(ctx: Ctx, a) -> int:
    print_status(ctx, a.run)
    return 0


def cmd_init(ctx: Ctx, a) -> int:
    print("To get a free key: https://console.cloud.google.com/apis/credentials\n"
          "  create a project, enable the YouTube Data API v3, create an API key,\n"
          "  restrict it to that API, and leave Application restrictions on None.")
    key = getpass.getpass("Paste the key (it will not show): ").strip()
    if not config.valid_api_key(key):
        raise ValueError("that does not look like an API key: it is one long word of letters, digits, - and _")
    check_key("youtube", key, Budget(ctx.store, "youtube", 10_000))
    print(f"The key works. Saved in {config.save_api_key(key)}, readable by you only.")
    print("next: smdc dashboard")
    return 0


def cmd_doctor(ctx: Ctx, a) -> int:
    """Each line is ok, or says what to do. Exit code 1 when something stops a collection."""
    bad = 0

    def line(ok: bool, text: str, fix: str = "", blocking: bool = True) -> None:
        nonlocal bad
        bad += (not ok) and blocking
        print(("ok    " if ok else "ERROR " if blocking else "note  ") + text + (f"\n      {fix}" if fix and not ok else ""))

    line(sys.version_info >= (3, 11), f"Python {sys.version.split()[0]}", "Python 3.11 or newer is required.")
    private = os.name != "posix" or not stat.S_IMODE(ctx.data.stat().st_mode) & 0o077
    line(private, f"data folder {ctx.data}", f"Others can read it: chmod 700 {ctx.data}", blocking=False)
    env = config.user_env_path()
    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    line(bool(key), "YouTube API key found" if key else "no YouTube API key", "Run: smdc init")
    if env.is_file():
        secret = os.name != "posix" or not stat.S_IMODE(env.stat().st_mode) & 0o077
        line(secret, f"key file {env}", f"Others can read it: chmod 600 {env}", blocking=False)
    if a.online and key:
        try:
            check_key("youtube", key, Budget(ctx.store, "youtube", 10_000))
            line(True, "YouTube accepts the key (1 unit)")
        except ValueError as e:
            line(False, "YouTube refuses the key", str(e))
    for module, feature in (("lingua", "language detection"), ("pyarrow", "Parquet export")):
        line(importlib.util.find_spec(module) is not None, feature, 'Optional: pip install "smdc[all]"', blocking=False)
    today = retention.dt.date.today().isoformat()
    late = [r["name"] for r in ctx.store.runs() if (retention.delete_by(r["oldest_at"]) or "9") < today]
    line(not late, "30-day rule: nothing overdue" if not late else f"30-day rule: overdue: {', '.join(late)}",
         "Refresh these runs and delete their older snapshots, or delete them: smdc delete NAME", blocking=False)
    return 1 if bad else 0


def cmd_delete(ctx: Ctx, a) -> int:
    run = ctx.store.run(a.run)
    what = "the older snapshots of" if a.older_snapshots else "everything collected in"
    extra = "" if a.keep_exports else " and its exports"
    if not a.yes and input(f"Delete {what} {run['name']!r}{extra}? This cannot be undone. [y/N] ").strip().lower() != "y":
        print("nothing deleted")
        return 1
    fn = retention.delete_older_snapshots if a.older_snapshots else retention.delete_run
    out = fn(ctx.store, ctx.data, run["name"], not a.keep_exports)
    print(f"deleted {out['snapshots']} snapshot(s) and {len(out['exports'])} export folder(s)")
    return 0


def cmd_dashboard(ctx: Ctx, a) -> int:
    from .dashboard.server import serve
    serve(ctx, a.port, not a.no_browser)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="smdc", description=__doc__.split("\n")[0], epilog=EXAMPLES,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", default=os.environ.get("SMDC_DATA") or str(DEFAULT_DATA),
                   help=f"data folder (default $SMDC_DATA or {DEFAULT_DATA})")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=f"smdc {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="save your YouTube API key, after testing it (1 unit)")
    s = sub.add_parser("doctor", help="check this computer's setup")
    s.add_argument("--online", action="store_true", help="also try the key on YouTube (1 unit)")
    s = sub.add_parser("estimate", help="resolve targets and estimate the quota cost")
    s.add_argument("config")
    s = sub.add_parser("run", help="create a run from a TOML file and collect")
    s.add_argument("config")
    s.add_argument("--force", action="store_true", help="start even above today's remaining quota")
    s.add_argument("--wait", action="store_true", help="sleep through quota resets until done")
    for name, hlp in (("resume", "continue a paused or incomplete run"),
                      ("refresh", "re-collect the same videos as a new snapshot")):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("run")
        s.add_argument("--wait", action="store_true", help="sleep through quota resets until done")
    s = sub.add_parser("process", help="redo text cleaning and language filtering (no API calls)")
    s.add_argument("run")
    s.add_argument("--config", help="take the [process] section from this TOML file")
    s = sub.add_parser("export", help="write data, report, manifest and codebook")
    s.add_argument("run")
    s.add_argument("--format", choices=("csv", "jsonl", "parquet"), default="csv")
    s.add_argument("--kept", action="store_true", help="only items that passed the language filter")
    s.add_argument("--excel", action="store_true", help="';' separator + BOM for Excel")
    s.add_argument("--snapshot", type=int, help="snapshot number (default: latest)")
    s.add_argument("--out", help="parent folder (default DATA/exports)")
    s = sub.add_parser("status", help="list runs, or detail one run")
    s.add_argument("run", nargs="?")
    s = sub.add_parser("delete", help="delete a run or its older snapshots, for good (30-day rule)")
    s.add_argument("run")
    s.add_argument("--older-snapshots", action="store_true", help="keep the latest snapshot")
    s.add_argument("--keep-exports", action="store_true", help="leave the exports in DATA/exports")
    s.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    s = sub.add_parser("dashboard", help="local web dashboard")
    s.add_argument("--port", type=int, default=8777)
    s.add_argument("--no-browser", action="store_true")

    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    ctx = Ctx(Path(a.data))
    try:
        return globals()[f"cmd_{a.cmd}"](ctx, a)
    except net.HttpError as e:
        print(f"error: {explain_error(e)}", file=sys.stderr)
        return 2
    except FileNotFoundError as e:
        print(f"error: no such file: {e.filename}", file=sys.stderr)
        return 2
    except (KeyError, ValueError, RuntimeError) as e:
        if a.verbose:
            raise
        print(f"error: {e.args[0] if e.args else e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

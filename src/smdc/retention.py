"""Deleting collected data, and the date by which it must go.

YouTube's API policies ask that data stored for more than 30 days be refreshed or deleted. A
refresh writes a new snapshot and leaves the older ones as they were, so each snapshot has its own
date. Deleting really removes the data from the database file, and the quota ledger stays: it is a
history of what the key spent, with no collected content.
"""

import datetime as dt
import json
import shutil
from pathlib import Path

from .store import Store

RETENTION_DAYS = 30


def delete_by(ts: str | None) -> str | None:
    """The last day a snapshot stamped `ts` (UTC, ISO) may be kept, as YYYY-MM-DD."""
    if not ts:
        return None
    t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00")) + dt.timedelta(days=RETENTION_DAYS)
    return t.strftime("%Y-%m-%d")


def snapshot_delete_by(snap: dict) -> str | None:
    return delete_by(snap.get("finished_at") or snap.get("started_at"))


def _manifests_of(out_root: Path, run_name: str, snaps: list[dict]) -> list[tuple[Path, dict]]:
    """The export folders holding these snapshots, with their manifest.

    A folder's name is not enough: a run may be called `a-s2`, and a run deleted earlier may have
    left exports under the same name as this one.
    """
    wanted = {(s["seq"], s["started_at"]) for s in snaps}
    found = []
    root = out_root.resolve()
    if not root.is_dir():
        return found
    for folder in sorted(root.iterdir()):
        manifest = folder / "run_manifest.json"
        if folder.is_symlink() or not folder.is_dir() or not manifest.is_file():
            continue
        try:
            m = json.loads(manifest.read_text(encoding="utf-8"))
            key = (m["snapshot"]["seq"], m["snapshot"]["started_at"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if m.get("run") == run_name and key in wanted:
            found.append((folder, m))
    return found


def exports_of(out_root: Path, run_name: str, snaps: list[dict]) -> list[Path]:
    """The export folders holding these snapshots."""
    return [folder for folder, _ in _manifests_of(out_root, run_name, snaps)]


def list_exports(out_root: Path, run_name: str, snaps: list[dict]) -> list[dict]:
    """What the page shows of each export of a run, newest first."""
    rows = [{"folder": folder.name, "path": str(folder), "snapshot": m["snapshot"]["seq"],
             "exported_at": m.get("exported_at"), **{k: m.get("export", {}).get(k) for k in ("format", "kept_only", "rows")}}
            for folder, m in _manifests_of(out_root, run_name, snaps)]
    return sorted(rows, key=lambda r: r["exported_at"] or "", reverse=True)


def _remove_exports(data: Path, run_name: str, snaps: list[dict]) -> list[str]:
    folders = exports_of(data / "exports", run_name, snaps)
    for folder in folders:
        shutil.rmtree(folder)
    return [f.name for f in folders]


def delete_run(store: Store, data: Path, ref: str | int, exports: bool = True) -> dict:
    """Deletes a run: its snapshots, comments, raw responses and log, and its config file.

    Args:
        store: Project database.
        data: The data folder (holds `exports/` and `runs/`).
        ref: Run name or id.
        exports: Also delete the exports of this run found in the data folder.

    Returns:
        {"snapshots": n, "exports": [names of the folders removed]}.
    """
    run = store.run(ref)
    snaps = store.snapshots(run["run_id"])
    removed = _remove_exports(data, run["name"], snaps) if exports else []
    store.delete_run(run["run_id"])
    (data / "runs" / f"{run['name']}.toml").unlink(missing_ok=True)
    return {"snapshots": len(snaps), "exports": removed}


def delete_older_snapshots(store: Store, data: Path, ref: str | int, exports: bool = True) -> dict:
    """Deletes every snapshot of a run but the latest, the one a refresh leaves behind.

    Args:
        store: Project database.
        data: The data folder (holds `exports/`).
        ref: Run name or id.
        exports: Also delete the exports of those snapshots found in the data folder.

    Returns:
        {"snapshots": n, "exports": [names of the folders removed]}.
    """
    run = store.run(ref)
    older = store.snapshots(run["run_id"])[:-1]
    removed = _remove_exports(data, run["name"], older) if exports else []
    if older:
        store.delete_snapshots([s["snapshot_id"] for s in older])
    return {"snapshots": len(older), "exports": removed}

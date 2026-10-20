"""Analysis-ready exports: data tables plus the documents that describe them.

An export folder holds `containers`, `items` (and `gone` for refreshes) as
CSV/JSONL/Parquet, `exclusion_report.json`, `run_manifest.json` and
`codebook.md`, so a dataset can be shared or archived on its own.
"""

import csv
import datetime as dt
import json
from pathlib import Path

from . import __version__
from .pipeline import coverage, process_snapshot, run_config
from .store import Store

ITEM_FIELDS = {
    "item_id": "Platform comment ID.",
    "container_id": "ID of the video the comment was posted under.",
    "thread_id": "ID of the thread's top-level comment (equals item_id for top-level comments).",
    "parent_id": "ID of the comment replied to; empty for top-level comments.",
    "depth": "0 = top-level comment, 1 = reply.",
    "author_key": "Pseudonymous author key: HMAC-SHA256 of the platform user ID with a "
                  "project secret (stable across runs; not reversible without the secret).",
    "author_id": "Raw platform user ID (only when privacy.keep_identifiers = true).",
    "author_name": "Display name (only when privacy.keep_identifiers = true).",
    "text_raw": "Text exactly as returned by the API (only when privacy.keep_identifiers = true, "
                "since it contains @handles).",
    "text": "Text on one line (NFC, whitespace collapsed; @handles -> @user if redact_mentions).",
    "text_clean": "text without emoji/symbols and punctuation (intra-word apostrophes kept), "
                  "for lexicon or topic models.",
    "lang": "Most likely language (ISO 639-1) among process.candidates; empty if undetectable; "
            "'en' also when a short text was kept by the English word-list fallback.",
    "lang_conf": "Relative confidence of lang (0-1) among the candidate languages.",
    "excluded_reason": "Empty if kept; else empty_after_cleaning, other_language or undetected.",
    "like_count": "Likes at collected_at.",
    "reply_count": "Replies reported by the platform at collected_at (top-level comments).",
    "created_at": "Posting time, UTC ISO-8601.",
    "updated_at": "Last edit time, UTC ISO-8601.",
    "collected_at": "When this record was fetched, UTC ISO-8601.",
}
CONTAINER_FIELDS = {
    "container_id": "Video ID.", "position": "Order in the target list.",
    "availability": "ok or not_found at resolution time.",
    "status": "done, unavailable (see error), or an unfinished phase.",
    "error": "Why the video was skipped or failed (e.g. comments_disabled).",
    "title": "Video title.", "author_id": "Uploader channel ID.", "author_name": "Uploader channel name.",
    "published_at": "Video publication time, UTC.",
    "comment_count": "Platform comment count (replies included) at resolved_at; empty if hidden.",
    "stats": "Other counters at resolved_at (JSON: viewCount, likeCount...).",
    "resolved_at": "When the metadata was fetched.",
    "comments": "Top-level comments collected (before language filtering).",
    "replies": "Replies collected (before language filtering).",
}
GONE_FIELDS = {"item_id": "Comment sampled in the previous snapshot and no longer public.",
               "container_id": "Video ID.", "prev_snapshot": "Snapshot number it was last seen in."}
PRIVATE = ("author_id", "author_name", "text_raw")


def export(store: Store, run_ref: str, out_root: Path, fmt: str = "csv", kept_only: bool = False,
           excel: bool = False, seq: int | None = None) -> Path:
    """Writes one snapshot of a run to `out_root/<run>[-s<seq>]/`.

    Args:
        store: Project database.
        run_ref: Run name or id.
        out_root: Parent folder of the export.
        fmt: "csv", "jsonl" or "parquet".
        kept_only: Leave out items with an exclusion reason.
        excel: CSV with ';' separators and a BOM, as French Excel expects; text starting
            with = + - @ gets a leading ' so that Excel shows it instead of running it.
        seq: Snapshot number; the latest by default.

    Returns:
        The export folder.
    """
    run = store.run(run_ref)
    cfg = run_config(store, run["run_id"])
    snaps = store.snapshots(run["run_id"])
    snap = snaps[-1] if seq is None else next(s for s in snaps if s["seq"] == seq)
    sid = snap["snapshot_id"]
    if store.q("SELECT 1 FROM items WHERE snapshot_id = ? AND text_norm IS NULL LIMIT 1", (sid,)):
        process_snapshot(store, sid, cfg)

    item_cols = [c for c in ITEM_FIELDS if cfg.privacy.keep_identifiers or c not in PRIVATE]
    sel = ", ".join("text_norm AS text" if c == "text" else c for c in item_cols)
    where = " AND excluded_reason IS NULL" if kept_only else ""
    items = store.q(f"""SELECT {sel} FROM items WHERE snapshot_id = ? AND selected = 1{where}
                        ORDER BY container_id, thread_id, depth, created_at""", (sid,))
    containers = store.q("""SELECT container_id, position, availability,
            CASE WHEN phase = 'unavailable' THEN 'unavailable' ELSE phase END AS status, error,
            title, author_id, author_name, published_at, comment_count, stats, resolved_at,
            threads AS comments, replies FROM containers WHERE snapshot_id = ? ORDER BY position""",
                         (sid,))
    gone = store.q("""SELECT g.item_id, g.container_id, s.seq AS prev_snapshot FROM gone g
                      JOIN snapshots s ON s.snapshot_id = g.prev_snapshot_id
                      WHERE g.snapshot_id = ?""", (sid,))

    out = out_root / (run["name"] + (f"-s{snap['seq']}" if len(snaps) > 1 else ""))
    out.mkdir(parents=True, exist_ok=True)
    write = {"csv": lambda p, r, c: _csv(p, r, c, excel), "jsonl": _jsonl, "parquet": _parquet}[fmt]
    write(out / f"containers.{fmt}", containers, list(CONTAINER_FIELDS))
    write(out / f"items.{fmt}", items, item_cols)
    if snap["seq"] > 1:
        write(out / f"gone.{fmt}", gone, list(GONE_FIELDS))

    report = {**store.report(sid), "coverage": coverage(store, run["run_id"], sid)}
    _json(out / "exclusion_report.json", report)
    _json(out / "run_manifest.json", {
        "tool": "socialcollect", "tool_version": __version__, "run": run["name"],
        "platform": run["platform"], "created_by_version": run["tool_version"],
        "snapshot": {k: snap[k] for k in ("seq", "status", "started_at", "finished_at", "units")},
        "snapshots": len(snaps), "config": cfg.to_dict(),
        "exported_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "export": {"format": fmt, "kept_only": kept_only, "rows": len(items)},
        "containers": {c["container_id"]: c["status"] + (f" ({c['error']})" if c["error"] else "")
                       for c in containers},
        "retention_note": "YouTube API Services Developer Policies: API data stored for more "
                          "than 30 days must be refreshed or deleted (refresh this run).",
    })
    _codebook(out / "codebook.md", run["name"], item_cols, snap["seq"] > 1)
    return out


FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _defuse(v):
    """Prefixes a quote to text Excel would run as a formula (a comment can be '=HYPERLINK(...)')."""
    return "'" + v if isinstance(v, str) and v.startswith(FORMULA_START) else v


def _csv(path: Path, rows: list[dict], cols: list[str], excel: bool) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig" if excel else "utf-8") as f:
        w = csv.DictWriter(f, cols, delimiter=";" if excel else ",", extrasaction="ignore")
        w.writeheader()
        w.writerows([{k: _defuse(v) for k, v in r.items()} for r in rows] if excel else rows)


def _jsonl(path: Path, rows: list[dict], cols: list[str]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps({c: r.get(c) for c in cols}, ensure_ascii=False) + "\n")


def _parquet(path: Path, rows: list[dict], cols: list[str]) -> None:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as e:
        raise RuntimeError("parquet export needs pyarrow: pip install 'socialcollect[parquet]'") from e
    pq.write_table(pa.Table.from_pylist([{c: r.get(c) for c in cols} for r in rows]), path)


def _json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _codebook(path: Path, name: str, item_cols: list[str], with_gone: bool) -> None:
    lines = [f"# Codebook: {name}", "",
             "Generated by socialcollect. See run_manifest.json for the exact parameters.", ""]
    tables = [("containers", CONTAINER_FIELDS), ("items", {c: ITEM_FIELDS[c] for c in item_cols})]
    if with_gone:
        tables.append(("gone", GONE_FIELDS))
    for table, fields in tables:
        lines += [f"## {table}", "", "| column | meaning |", "|---|---|"]
        lines += [f"| `{c}` | {d} |" for c, d in fields.items()] + [""]
    path.write_text("\n".join(lines), encoding="utf-8")

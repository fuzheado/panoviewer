"""Prune the panoviewer cache.

Two kinds of rubbish accumulate there, both created by the tiler:

  1. **`<md5>.full.jpg`** — the full-resolution download, used only while generate.py runs. The old
     `multires.sh` never deleted it. Measured on the live tool 2026-10-23: **7,916 files, 206 GB**,
     two thirds of the 303 GB cache.
  2. **Tile directories with no `config.json`** — an interrupted run. 891 of those existed; the viewer
     treats each as "tiling still in progress", so it polls for ever.

Both are now handled at the source (`tile.py` removes the temporary in a `finally`, and deletes partial
output on failure), so this script exists for the backlog and for the occasional corpse.

Safety rules, because this deletes real data:

  * **dry run by default** — it prints what it would remove and exits 0 without touching anything;
  * `--apply` is required to delete, and the first line of output says which mode it is in;
  * `--min-age` (default 1 hour) protects files a *running* tiling job could still be using — a job that
    started a minute ago must not have its inputs pulled out from under it;
  * a tile directory is only ever "abandoned" if it has **no `config.json`**; a finished one is never a
    candidate, no matter how old;
  * flat `.jpg` previews and finished tile sets are **never** removed by default. `--max-bytes` opts in to
    LRU eviction to a size budget, oldest first, and says so in the report.
"""
import argparse
import json
import os
import shutil
import sys
import time

TEMP_SUFFIXES = (".full.jpg", ".full.jpg.part")
# "<md5>.tiling" marks when a tiling attempt was last made (see panoviewer.TILING_RETRY_SECONDS).
# Harmless, but they are directory entries and this cache has 22k of those already.
MARKER_SUFFIX = ".tiling"
MARKER_MAX_AGE = 30 * 86400


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def _tree_bytes(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def scan(cache_dir, min_age=3600, now=None, max_bytes=None):
    """Return a report of what *would* be removed. Reads only."""
    now = now if now is not None else time.time()
    report = {
        "cache_dir": cache_dir,
        "min_age_seconds": min_age,
        "temporaries": [],        # (path, bytes)
        "stale_markers": [],      # (path, bytes) — old "<md5>.tiling" attempt markers
        "abandoned_tiles": [],    # (path, bytes)
        "lru": [],                # (path, bytes) — only with max_bytes
        "kept": {"flat_jpg": 0, "complete_tiles": 0, "too_recent": 0},
    }
    try:
        entries = sorted(os.listdir(cache_dir))
    except FileNotFoundError:
        return report

    reclaimable = {"flat": [], "tiles": []}
    for name in entries:
        path = os.path.join(cache_dir, name)
        age = now - _mtime(path)

        if name.endswith(MARKER_SUFFIX):
            if age >= MARKER_MAX_AGE:
                report["stale_markers"].append((path, os.path.getsize(path)))
            continue

        if name.endswith(TEMP_SUFFIXES):
            if age >= min_age:
                report["temporaries"].append((path, os.path.getsize(path)))
            else:
                report["kept"]["too_recent"] += 1
            continue

        if os.path.isdir(path):
            finished = os.path.isfile(os.path.join(path, "config.json"))
            if finished:
                report["kept"]["complete_tiles"] += 1
                if max_bytes:
                    reclaimable["tiles"].append((path, _tree_bytes(path), _mtime(path)))
            elif age >= min_age:
                report["abandoned_tiles"].append((path, _tree_bytes(path)))
            else:
                report["kept"]["too_recent"] += 1
            continue

        if name.endswith(".jpg"):
            report["kept"]["flat_jpg"] += 1
            if max_bytes:
                reclaimable["flat"].append((path, os.path.getsize(path), _mtime(path)))

    if max_bytes:
        total = sum(size for _p, size in report["temporaries"]) \
            + sum(size for _p, size in report["abandoned_tiles"]) \
            + sum(size for _p, size, _m in reclaimable["flat"]) \
            + sum(size for _p, size, _m in reclaimable["tiles"])
        if total > max_bytes:
            candidates = sorted(reclaimable["flat"] + reclaimable["tiles"], key=lambda item: item[2])
            excess = total - max_bytes
            for path, size, _mtime_ in candidates:
                if excess <= 0:
                    break
                report["lru"].append((path, size))
                excess -= size
    return report


def _total(report):
    everything = (report["temporaries"] + report["stale_markers"]
                  + report["abandoned_tiles"] + report["lru"])
    return len(everything), sum(size for _p, size in everything)


def apply(report, log=print):
    removed, freed = 0, 0
    for path, size in (report["temporaries"] + report["stale_markers"]
                       + report["abandoned_tiles"] + report["lru"]):
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            removed += 1
            freed += size
        except FileNotFoundError:
            pass
        except OSError as exc:
            log("  could not remove %s: %s" % (path, exc))
    return removed, freed


def _human(size):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1000 or unit == "TB":
            return "%.1f %s" % (size, unit)
        size /= 1000.0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prune the panoviewer cache (dry run by default).")
    parser.add_argument("--cache", default=os.path.join(os.environ.get("HOME", "."), "public_html", "cache"))
    parser.add_argument("--min-age", type=int, default=3600,
                        help="seconds a file must be untouched before it is a candidate (default 3600)")
    parser.add_argument("--max-bytes", type=int, default=None,
                        help="opt in to LRU eviction of finished entries down to this many bytes")
    parser.add_argument("--apply", action="store_true", help="actually delete (default: report only)")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    args = parser.parse_args(argv)

    report = scan(args.cache, min_age=args.min_age, max_bytes=args.max_bytes)
    count, freed = _total(report)

    if args.json:
        payload = {
            "mode": "apply" if args.apply else "dry-run",
            "cache_dir": report["cache_dir"],
            "temporaries": len(report["temporaries"]),
            "abandoned_tile_dirs": len(report["abandoned_tiles"]),
            "stale_markers": len(report["stale_markers"]),
            "lru_evictions": len(report["lru"]),
            "candidates": count,
            "bytes": freed,
            "kept": report["kept"],
        }
        if args.apply:
            removed, freed_actual = apply(report, log=lambda m: None)
            payload["removed"] = removed
            payload["bytes_freed"] = freed_actual
        print(json.dumps(payload, indent=2))
        return 0

    print("mode: %s" % ("APPLY (deleting)" if args.apply else "dry run (nothing is deleted)"))
    print("cache: %s   min-age: %ds" % (report["cache_dir"], args.min_age))
    print("  tiler temporaries (*.full.jpg)      : %6d  %10s" % (
        len(report["temporaries"]), _human(sum(s for _p, s in report["temporaries"]))))
    print("  stale tiling markers (>30 days)      : %6d  %10s" % (
        len(report["stale_markers"]), _human(sum(s for _p, s in report["stale_markers"]))))
    print("  abandoned tile dirs (no config.json): %6d  %10s" % (
        len(report["abandoned_tiles"]), _human(sum(s for _p, s in report["abandoned_tiles"]))))
    if args.max_bytes:
        print("  LRU evictions (--max-bytes)         : %6d  %10s" % (
            len(report["lru"]), _human(sum(s for _p, s in report["lru"]))))
    print("  kept: %(flat_jpg)d previews, %(complete_tiles)d finished tile sets, "
          "%(too_recent)d too recent" % report["kept"])
    print("  TOTAL: %d candidates, %s" % (count, _human(freed)))

    if not args.apply:
        print("\nNothing was deleted. Re-run with --apply to remove these.")
        return 0

    removed, freed_actual = apply(report)
    print("\nremoved %d entries, freed %s" % (removed, _human(freed_actual)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

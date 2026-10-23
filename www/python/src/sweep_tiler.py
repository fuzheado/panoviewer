"""Proactively tile panoramas that were left without a high-resolution version.

The on-demand retry in `panoviewer.build_config` fixes an entry the next time somebody *looks* at it.
This sweep fixes the ones nobody looks at: it walks a Commons category, and for every file that is
large enough to need tiling but has no `config.json` in the cache, it queues a job.

Why the category API rather than the database: the tool's cache is keyed by `md5(file name)`, which
cannot be reversed, so the cache alone cannot tell us *which* file an untitled entry belongs to. The
API hands back the file name and the size in the same response, so no query against the 100M-row
`image` table is needed (a `WHERE img_width > 4000` there is a full scan).

Safety and manners:

  * **dry run by default** — it prints what it would queue and exits; `--apply` is required to submit;
  * `--limit` bounds the files examined per run and `--cursor` carries the API continuation, so the
    walk is resumable and a run is always bounded work;
  * `--interval` paces API requests (default 1.0 s, per the Wikimedia User-Agent/etiquette rules);
  * entries that reached `panoviewer.TILING_MAX_ATTEMPTS` are skipped — the sweep must not resurrect a
    retry loop the handler deliberately gave up on;
  * entries whose tiles already exist, and files too small to need tiling, are never submitted.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import panoviewer                                                   # noqa: E402

API = "https://commons.wikimedia.org/w/api.php"
DEFAULT_CATEGORY = "Category:360° panoramas"
USER_AGENT = "panoviewer-sweep/1.0 (https://panoviewer.toolforge.org/)"


def fetch_page(category, continuation=None, limit=50, interval=1.0, opener=None, log=print):
    """One API page: file names plus sizes."""
    params = {
        "action": "query", "format": "json", "formatversion": "2",
        "generator": "categorymembers", "gcmtitle": category, "gcmtype": "file",
        "gcmlimit": str(limit), "prop": "imageinfo", "iiprop": "size",
    }
    if continuation:
        params.update(continuation)
    url = API + "?" + urllib.parse.urlencode(params)
    open_url = opener or (lambda u: urllib.request.urlopen(
        urllib.request.Request(u, headers={"User-Agent": USER_AGENT}), timeout=60))
    started = time.time()
    with open_url(url) as response:
        payload = json.loads(response.read().decode("utf-8"))
    waited = time.time() - started
    if interval > waited:
        time.sleep(interval - waited)
    pages = payload.get("query", {}).get("pages", [])
    return pages, payload.get("continue")


def plan(cache_dir, pages, attempts_max=None, include_unseen=False):
    """Decide what to do with each file. Reads the cache, writes nothing.

    `include_unseen=False` (the default) restricts the sweep to entries the tool has already served —
    i.e. it repairs panoramas somebody actually looked at and found hazy. Tiling the whole category
    instead would pre-generate high-resolution versions for ~1.5M files nobody has opened, which is a
    cache-growth policy decision (tens of gigabytes) rather than a bug fix.
    """
    attempts_max = attempts_max or panoviewer.TILING_MAX_ATTEMPTS
    decisions = {"tiled": 0, "too_small": 0, "capped": 0, "no_info": 0,
                 "not_cached": 0, "not_equirect": 0, "queue": []}
    for page in pages:
        info = (page.get("imageinfo") or [{}])[0]
        title = page.get("title", "")
        if title.startswith("File:"):
            title = title[len("File:"):]
        width = int(info.get("width") or 0)
        height = int(info.get("height") or 0)
        if not width:
            decisions["no_info"] += 1
            continue
        name = panoviewer.normalize_name(title)
        md5 = panoviewer.md5_name(name)
        if os.path.isfile(os.path.join(cache_dir, md5, "config.json")):
            decisions["tiled"] += 1
            continue
        if width <= panoviewer.MAX_WIDTH:
            decisions["too_small"] += 1
            continue
        if not panoviewer.is_full_equirectangular(width, height):
            # a cylindrical strip or a partial panorama: generate.py cannot cube it, so a job here
            # would fail 5 times and be capped. Measured: 3 of 3 such jobs failed in ~1-4 s.
            decisions["not_equirect"] += 1
            continue
        if not include_unseen and not os.path.isfile(os.path.join(cache_dir, md5 + ".jpg")):
            # never viewed: nothing has been served, so nothing can look hazy
            decisions["not_cached"] += 1
            continue
        if panoviewer._attempt_count(_ContextShim(cache_dir), md5) >= attempts_max:
            decisions["capped"] += 1
            continue
        decisions["queue"].append({"name": name, "md5": md5, "width": width, "height": height})
    return decisions


class _ContextShim(object):
    """Just enough of a Context for the attempt-count helper."""

    def __init__(self, cache_dir):
        self.cache_dir = cache_dir


def submit(items, home, cache_dir, job_image=None, run=None, log=print):
    """Queue one tiling job per item, sized from the geometry, and record the attempt."""
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, timeout=60))
    ctx = panoviewer.Context(home=home, public_dir=os.path.dirname(cache_dir),
                             job_image=job_image, log=log)
    ctx.cache_dir = cache_dir          # Context derives public_dir/cache; trust the real path here
    submitted, failed = 0, 0
    for item in items:
        argv = panoviewer.job_argv(ctx, item["md5"], item["name"], item["width"], item["height"])
        try:
            done = run(argv)
            ok = getattr(done, "returncode", 0) == 0
        except Exception as exc:                                    # noqa: BLE001
            ok, done = False, exc
        if ok:
            submitted += 1
            # record the attempt WITHOUT submitting again: _submit_tiler would run the shim a second
            # time, which the API rejects as a duplicate job name and which used to look like a failure
            try:
                marker = panoviewer._marker_path(ctx, item["md5"])
                with open(marker, "w") as handle:
                    handle.write(str(panoviewer._attempt_count(ctx, item["md5"]) + 1))
            except OSError:
                pass
        else:
            failed += 1
            log("  could not queue %s: %s" % (item["name"], getattr(done, "stderr", done)))
    return submitted, failed


def main(argv=None):
    parser = argparse.ArgumentParser(description="Queue tiling for panoramas with no high-resolution version.")
    parser.add_argument("--cache", default=os.path.join(os.environ.get("HOME", "."), "public_html", "cache"))
    parser.add_argument("--category", default=DEFAULT_CATEGORY)
    parser.add_argument("--limit", type=int, default=500, help="files to examine this run (default 500)")
    parser.add_argument("--page-size", type=int, default=50, help="files per API request (max 50)")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between API requests")
    parser.add_argument("--cursor", default=None, help="file holding the API continuation token")
    parser.add_argument("--max-jobs", type=int, default=25, help="stop queueing after this many (default 25)")
    parser.add_argument("--include-unseen", action="store_true",
                        help="also tile files nobody has opened (pre-generates cache; off by default)")
    parser.add_argument("--apply", action="store_true", help="actually queue jobs (default: report only)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    cache_dir = os.path.abspath(args.cache)
    home = os.environ.get("TOOL_DATA_DIR") or os.environ.get("HOME") or "."
    continuation = None
    if args.cursor and os.path.isfile(args.cursor):
        with open(args.cursor) as handle:
            continuation = json.load(handle)

    totals = {"scanned": 0, "tiled": 0, "too_small": 0, "capped": 0, "no_info": 0,
              "not_cached": 0, "not_equirect": 0, "queued": 0, "failed": 0}
    while totals["scanned"] < args.limit:
        pages, continuation = fetch_page(args.category, continuation, args.page_size, args.interval)
        if not pages:
            break
        totals["scanned"] += len(pages)
        decisions = plan(cache_dir, pages, include_unseen=args.include_unseen)
        for key in ("tiled", "too_small", "capped", "no_info", "not_cached", "not_equirect"):
            totals[key] += decisions[key]
        if args.apply and decisions["queue"]:
            room = args.max_jobs - totals["queued"]
            if room > 0:
                submitted, failed = submit(decisions["queue"][:room], home, cache_dir)
                totals["queued"] += submitted
                totals["failed"] += failed
        else:
            for item in decisions["queue"]:
                print("  would queue: %s (%dx%d)" % (item["name"], item["width"], item["height"]))
            totals["queued"] += len(decisions["queue"])
        # --max-jobs bounds *queueing*. In a dry run it must not cut the walk short, or the report
        # would understate the backlog.
        if not continuation or (args.apply and totals["queued"] >= args.max_jobs):
            break

    if args.cursor and continuation:
        with open(args.cursor, "w") as handle:
            json.dump(continuation, handle)

    if args.json:
        totals["mode"] = "apply" if args.apply else "dry-run"
        print(json.dumps(totals, indent=2))
        return 0
    print("mode: %s" % ("APPLY (queueing jobs)" if args.apply else "dry run (nothing is queued)"))
    print("  examined      : %(scanned)d files from %(category)s" % dict(totals, category=args.category))
    print("  already tiled : %(tiled)d" % totals)
    print("  too small     : %(too_small)d  (never tiled by design, <= %(limit)s px)"
          % dict(totals, limit=panoviewer.MAX_WIDTH))
    print("  gave up earlier: %(capped)d  (reached the %(limit)s-attempt cap)"
          % dict(totals, limit=panoviewer.TILING_MAX_ATTEMPTS))
    print("  no size info  : %(no_info)d" % totals)
    print("  never viewed  : %(not_cached)d  (skipped: nothing served, nothing to repair)" % totals)
    print("  not 2:1       : %(not_equirect)d  (partial/cylindrical: cannot be cubed, shown as-is)" % totals)
    print("  %(label)s: %(queued)d   failed: %(failed)d"
          % dict(totals, label="queued" if args.apply else "would queue"))
    if continuation:
        print("  cursor saved  : %s (next run continues here)" % args.cursor)
    return 0


if __name__ == "__main__":
    sys.exit(main())

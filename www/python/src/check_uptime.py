"""Hourly health check for panoviewer — the alerting the platform does not give you.

Designed to run as a **scheduled Toolforge job with `--emails onfailure`**, because Toolforge has no
alerting of its own: this tool sat dead for three days in October 2026 and nothing said so. It also
appends a line to `<TOOL_DATA_DIR>/logs/uptime.log`, so there is a local record whatever the mail
delivery does.

    toolforge jobs run uptime-check --schedule "@hourly" --image python3.11 --mount=all \
        --emails onfailure --command 'python3 $HOME/www/python/src/check_uptime.py'

What it asserts — the whole path, not just "a file was served":

  1. `/healthz?db=1` answers 200 with a JSON body whose `db_probe.ok` is true (the replica is reachable
     from the pod — the failure that took the tool down in the first place was not the database, but a
     deployment that could not start at all, so both matter);
  2. `/config` for a known panorama returns a *tiled* answer (`multiRes`), i.e. the handler, the cache
     and the database agree;
  3. one tile of that panorama is fetchable as `image/jpeg` — the path the browser actually follows.

Exit code is the signal: 0 healthy, 1 with a one-line reason. `--emails onfailure` turns that into mail.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://panoviewer.toolforge.org"
# A 16,416x8,208 panorama whose tiles have existed since 2026-10-23. If it ever stops being tiled this
# check will fail — which is the point, but the message says which step failed so it is not a mystery.
SAMPLE = "360-Panorama_(iotw2630b).jpg"
SAMPLE_TILE = "cache/759861e6416246b83dd556dbd48032bc/5/f0_0.jpg"
USER_AGENT = "panoviewer-uptime/1.0 (https://panoviewer.toolforge.org/)"


def _default_get(url, timeout=30):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as error:              # a 5xx must be reported, not raised
        return error.code, error.headers.get("Content-Type", ""), error.read()


def run(base=None, sample=None, tile=None, get=None, log=print):
    """Return (ok, message). `get` is injectable so the check itself can be tested."""
    base = (base or BASE).rstrip("/")
    sample = sample or SAMPLE
    tile = tile or SAMPLE_TILE
    get = get or _default_get

    status, _ctype, body = get(base + "/healthz?db=1")
    if status != 200:
        return False, "healthz answered HTTP %s" % status
    try:
        health = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
    except Exception as exc:                              # noqa: BLE001
        return False, "healthz body is not JSON (%s)" % exc
    if not health.get("public_dir_exists"):
        return False, "the docroot is missing inside the pod"
    probe = health.get("db_probe") or {}
    if not probe.get("ok"):
        return False, "database probe failed: %s" % probe.get("error", "no detail")

    status, _ctype, body = get("%s/config?f=%s" % (base, urllib.parse.quote(sample, safe="")))
    if status != 200:
        return False, "/config answered HTTP %s" % status
    try:
        payload = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
    except Exception as exc:                              # noqa: BLE001
        return False, "/config body is not JSON (%s)" % exc
    if "multiRes" not in (payload.get("pannellum") or {}):
        return False, "%s is not tiled (response: %s)" % (
            sample, json.dumps(payload)[:120])

    status, ctype, body = get("%s/%s" % (base, tile))
    if status != 200:
        return False, "tile answered HTTP %s" % status
    if "image" not in (ctype or ""):
        return False, "tile served as %r, not an image" % (ctype,)
    if not body:
        return False, "tile was empty"

    return True, "ok: python %s, db ok, tiles serving (%d bytes)" % (
        health.get("python", "?"), len(body))


def _tree_bytes(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def survey(cache_dir):
    """One pass over the cache: how many panoramas are tiled, how many are previews only, how big."""
    counts = {"tiled": 0, "untitled": 0, "previews": 0, "bytes": 0}
    try:
        names = os.listdir(cache_dir)
    except OSError:
        return counts
    for name in names:
        path = os.path.join(cache_dir, name)
        try:
            if os.path.isdir(path):
                if os.path.isfile(os.path.join(path, "config.json")):
                    counts["tiled"] += 1
                else:
                    counts["untitled"] += 1
                counts["bytes"] += _tree_bytes(path)
            elif name.endswith(".jpg") and not name.endswith(".full.jpg"):
                counts["previews"] += 1
                counts["bytes"] += os.path.getsize(path)
        except OSError:
            continue
    return counts


def trend(cache_dir=None, apply=True, log=print):
    """Append one line of *trend* rather than health.

    A number that would have made the October 2026 problems visible before anyone complained: `untitled`
    is the count of panoramas that have been served but never tiled (891 of them at the worst point), and
    `cache` is the figure the 200 GB budget enforces.
    """
    cache_dir = cache_dir or os.path.join(os.environ.get("TOOL_DATA_DIR") or os.environ.get("HOME") or ".",
                                          "public_html", "cache")
    counts = survey(cache_dir)
    message = "tiled=%d untitled=%d previews=%d cache=%.1fGB" % (
        counts["tiled"], counts["untitled"], counts["previews"], counts["bytes"] / 1e9)
    if apply:
        _append_log(True, message, tag="TREND")
    log("[trend] %s" % message)
    return counts


def _append_log(ok, message, tag=None):
    data_dir = os.environ.get("TOOL_DATA_DIR")
    if not data_dir:
        return
    try:
        path = os.path.join(data_dir, "logs", "uptime.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as handle:
            label = tag or ("OK  " if ok else "FAIL")
            handle.write("%s %s %s\n" % (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), label, message))
    except OSError:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description="Health check for the panoviewer tool.")
    parser.add_argument("--base", default=os.environ.get("PANOVIEWER_URL", BASE))
    parser.add_argument("--sample", default=SAMPLE)
    parser.add_argument("--tile", default=SAMPLE_TILE)
    parser.add_argument("--trend", action="store_true",
                        help="append a weekly cache summary to logs/uptime.log instead of checking health")
    args = parser.parse_args(argv)

    if args.trend:
        counts = trend()
        return 0 if counts["previews"] or counts["tiled"] else 1

    ok, message = run(base=args.base, sample=args.sample, tile=args.tile)
    line = "[uptime] %s" % message
    print(line, flush=True)
    _append_log(ok, message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

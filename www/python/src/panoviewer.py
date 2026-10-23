"""Core of the panoviewer request handler: a faithful port of public_html/config.php.

Why this module exists separately from `app.py`: the whole point of the port is that behaviour
must not change, and behaviour is only provable if it can be exercised without a database, a
network, or a cluster. Every side effect is therefore injected through `Context`:

    metadata(file_name)   -> {"width": int, "height": int, "timestamp": "YYYYMMDDHHMMSS"} or None
    fetch(url, limit)     -> bytes  (limit=None means "the whole body")
    submit_job(md5, argv) -> (ok: bool, detail: str)
    log(message)          -> None

Production wiring is at the bottom of this file; the tests in ../tests/ pass fakes.

The contract (measured from index.html and config.php, see php-probe/PORT-panoviewer-off-php.md):

    GET /config.php?f=<name>        -> {"multires_pending": bool, "pannellum": {...width, autoLoad}}
                                    or {"pannellum": {"multiRes": {...}, ...}, "width": N}
    GET /config.php?f=<name>&p=1    -> same shapes, but never fetches, tiles or refreshes
    errors                          -> {"error": "..."} with HTTP **200** (the JS checks the key)

Differences from the PHP, all deliberate and all tested:

  1. A re-upload removes the old tiled directory for real. PHP called rmdir(), which only removes
     an EMPTY directory, so stale tiles were never invalidated (php.net: "The directory must be
     empty"). Here: shutil.rmtree.
  2. The tiler subprocess' exit status is checked and logged. PHP discarded it, which is why a
     dead tiler looked like "a better version is being prepared" forever.
  3. varbinary columns are decoded. A Python driver returns bytes for img_timestamp, where mysqli
     gave a str; unhandled that is a TypeError inside json.dumps (found on the first staging pod).
"""
import hashlib
import json
import math
import mimetypes
import os
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request

MAX_WIDTH = 4000
# The interim preview shown while a panorama is being tiled. 4000 was the old choice and produced a
# 1.9-2.0 MB JPEG; the browser window is ~1920px wide, so 2048 is indistinguishable there for a third
# of the bytes. Tiling still starts at MAX_WIDTH.
PREVIEW_WIDTH = 2048
# How long a replica lookup may be reused. One /config costs a connect+SELECT (~116 ms measured from
# inside the pod) and the viewer polls while a panorama tiles, so this is the difference between a
# query every request and a query every half minute.
METADATA_TTL = 30

# If a panorama is still untitled after an attempt, try again -- but not on every request. The marker
# file lives beside the cache entry; see _attempt_due().
TILING_RETRY_SECONDS = 900
# ...and stop after this many attempts, or a file that can never tile (a non-2:1 image, say) would
# re-queue a job every window for ever, downloading tens of MB each time.
TILING_MAX_ATTEMPTS = 5
UPLOAD_BASE = "https://upload.wikimedia.org/wikipedia/commons"
THUMB_BASE = "https://commons.wikimedia.org/w/thumb.php"
# The jobs CLI resolves a type name ("python3.11") to an image reference; the shim below talks to
# the jobs API directly, so it must send the RESOLVED reference. Measured on staging 2026-10-23 with
# `toolforge jobs run --image python3.11` + `kubectl get job -o jsonpath="...image"`: the CLI sends
# this exact string. Sending the bare type made the API fall back to the tool's default image, which
# is the denylisted buildpack one -- i.e. every tiling job failed at admission.
DEFAULT_JOB_IMAGE = ("docker-registry.tools.wmflabs.org/toolforge-python311-sssd-web:latest")
USER_AGENT = "panoviewer/2.0 (https://panoviewer.toolforge.org/)"

# config.php sniffs the first 100 bytes at offset 6 for one of these.
JPEG_SIGNATURES = (
    (6, 4, b"JFIF"),
    (6, 4, b"Exif"),
    (6, 9, b"Photoshop"),
    (6, 20, b"http://ns.adobe.com/"),
)


# ── naming ────────────────────────────────────────────────────────────────────────────────────────
def ascii_ucfirst(text):
    """PHP's ucfirst(), which uppercases the first BYTE, not the first character.

    This is fidelity, not pedantry: md5(file name) selects the cache directory, so a name like
    "école.jpg" must map to the same md5 as PHP produced. str.upper() would yield "École.jpg" and a
    different md5, i.e. a cache miss for every non-ASCII-leading file.
    """
    if not text:
        return text
    first = text[0]
    if "a" <= first <= "z":
        return first.upper() + text[1:]
    return text


def normalize_name(raw):
    """config.php: str_replace(' ', '_', ucfirst($_GET['f']))."""
    return ascii_ucfirst((raw or "").replace(" ", "_"))


def md5_name(name):
    return hashlib.md5(name.encode("utf-8")).hexdigest()


def full_file_url(md5, name):
    return "%s/%s/%s/%s" % (UPLOAD_BASE, md5[0], md5[:2], urllib.parse.quote(name, safe=""))


def thumb_url(name, width):
    return "%s?w=%d&f=%s" % (THUMB_BASE, width, urllib.parse.quote(name, safe=""))


def shell_arg(text):
    """PHP's escapeshellarg(), byte for byte."""
    return "'" + str(text).replace("'", "'\\''") + "'"


def looks_like_jpeg(head):
    return any(head[offset:offset + size] == sig for offset, size, sig in JPEG_SIGNATURES)


# ── injected environment ─────────────────────────────────────────────────────────────────────────
class Context(object):
    def __init__(self, home, public_dir=None, data_dir=None, max_width=MAX_WIDTH,
                 metadata=None, fetch=None, submit_job=None, log=None,
                 job_image=None, tool_name=None, clock=None, preview_width=None):
        self.home = home
        self.data_dir = data_dir or home
        self.public_dir = public_dir or os.path.join(home, "public_html")
        self.max_width = max_width
        self.cache_dir = os.path.join(self.public_dir, "cache")
        # `is not None`, not `or`: a callable whose __len__ is 0 (MetadataCache) is falsy, so `or`
        # would silently substitute the stub. That is exactly what happened, and the hourly health
        # check reported "database probe failed: no detail" within minutes of the deploy.
        self.metadata = metadata if metadata is not None else (lambda name: None)
        self.fetch = fetch if fetch is not None else (lambda url, limit=None: b"")
        self.submit_job = submit_job if submit_job is not None else (
            lambda md5, argv: (True, "not submitted (stub)"))
        self.log = log if log is not None else (lambda message: None)
        self.job_image = job_image if job_image is not None else DEFAULT_JOB_IMAGE
        self.tool_name = tool_name or os.path.basename(home.rstrip("/")) or "panoviewer"
        self.clock = clock if clock is not None else (lambda: time.time())
        self.preview_width = preview_width if preview_width is not None else PREVIEW_WIDTH

    # paths, all relative to the docroot because the browser resolves them against the page
    def jpg_rel(self, md5):
        return "cache/%s.jpg" % md5

    def tile_rel(self, md5):
        return "cache/%s" % md5

    def jpg_abs(self, md5):
        return os.path.join(self.cache_dir, md5 + ".jpg")

    def tile_abs(self, md5):
        return os.path.join(self.cache_dir, md5)


def _mtime_stamp(path):
    return time.strftime("%Y%m%d%H%M%S", time.gmtime(os.path.getmtime(path)))


def _needs_refresh(ctx, jpg_path, db_timestamp):
    if not os.path.isfile(jpg_path):
        return True
    return str(db_timestamp) > _mtime_stamp(jpg_path)


class MetadataCache(object):
    """Memoise the replica lookup for `ttl` seconds.

    A miss is never cached: an image uploaded a moment ago must be visible immediately, and a wrong
    "no such file" is worse than one extra query.
    """

    def __init__(self, loader, ttl=None, clock=None):
        self._loader = loader
        self._ttl = METADATA_TTL if ttl is None else ttl
        self._clock = clock or time.time
        self._entries = {}

    def __call__(self, name):
        now = self._clock()
        hit = self._entries.get(name)
        if hit is not None and hit[0] > now:
            return hit[1]
        value = self._loader(name)
        if value is None:
            self._entries.pop(name, None)
        else:
            self._entries[name] = (now + self._ttl, value)
        return value

    def size(self):
        """Deliberately NOT `__len__`: an empty cache would then be falsy, and a truthiness test
        anywhere (we had one) would silently drop it."""
        return len(self._entries)


# ── the tiler ────────────────────────────────────────────────────────────────────────────────────
def job_argv(ctx, md5, name, width=None, height=None):
    """The argv `bin/toolforge` expects.

    Changed from the PHP's version on purpose: the tiler is now `tile.py` on the stock python3.11 image,
    run with the venv interpreter (Pillow/numpy) instead of a buildpack image with a PHP-bearing
    `Procfile`. The old image cannot run at all — the denylist blocks this tool's whole image namespace
    (`Regex=^.*/tool-panoviewer/.*$`), so the tiler had to stop being a buildpack image.
    """
    python = os.path.join(ctx.home, "www", "python", "venv", "bin", "python")
    tiler = os.path.join(ctx.home, "www", "python", "src", "tile.py")
    command = "%s %s %s %s %s" % (
        shell_arg(python),
        shell_arg(tiler),
        shell_arg(ctx.cache_dir),
        shell_arg(md5),
        shell_arg(urllib.parse.quote(name, safe="")),
    )
    geometry = tiling_geometry(width, height)
    if geometry and geometry[0] != "infer":
        command += " --haov %s --vaov %s" % geometry
    return [os.path.join(ctx.home, "bin", "toolforge"), "jobs", "run",
            "--image", ctx.job_image, "--mount=all",
            # without these two the job gets the API default of 512Mi / 0.1 CPU and is OOM-killed
            "--mem", "%dGi" % tiler_memory_gib(width, height),
            "--cpu", "1",
            "--command", command, "pano-" + md5]


def is_full_equirectangular(width, height, tolerance=0.02):
    """Only a 2:1 image is a full equirectangular panorama, which is what the tiler can cube.

    `generate.py` refuses anything else without `--haov`/`--vaov`:

        Unless given the --haov option, equirectangular input image must be a full (not partial) panorama!

    Those files therefore never tiled -- not on this code, and not on the PHP -- and the viewer promised
    "a better version is being prepared" for ever. A cylindrical strip (8:1) is the common case. They are
    still served, at their native size; there is simply no cube version to make.
    """
    if not width or not height:
        return False
    return abs(float(width) / height - 2.0) <= 2.0 * tolerance


def tiling_geometry(width, height):
    """Return (haov, vaov) to declare, or None if the image cannot be cubed.

    `generate.py` assumes 360x180 only when `width/height == 2` **exactly**, and exits otherwise; a
    panorama that is 2:1 to within a rounding error therefore failed with "must be a full (not partial)
    panorama". Declaring the coverage makes those tile, which is the intent. Anything further from 2:1
    is a cylindrical strip or a genuine partial panorama: no cube version exists for it.
    """
    if not is_full_equirectangular(width, height):
        return None
    if int(width) == 2 * int(height):
        return ("infer", "infer")      # exact 2:1: let generate.py infer it, as before
    return (360, 180)


def tiler_memory_gib(width, height=None):
    """Memory the tiling job must be given.

    Tiling a panorama is memory-bound, and the jobs default is **512Mi** (measured with the jobs API,
    2026-10-23). nona has to decode the source — `width * height * 3` bytes, 1.0 GB for a 26000x13000
    image — and write six cube faces at the cube resolution generate.py derives from the width. At
    512Mi the pod is SIGKILLed by the OOM killer: no traceback, no stderr, and `finally` never runs, so
    the `.full.jpg` temporary survives. That is why the biggest panoramas never produced a high
    resolution version, and part of why 206 GB of temporaries accumulated.

    Deliberately conservative and clamped to Toolforge's 4GiB ceiling.
    """
    width = int(width or 0)
    height = int(height or (width // 2))
    cube = 8 * int(width / math.pi / 8) if width else 0     # generate.py's rule
    needed = (width * height * 3 + 6 * cube * cube * 3) * 1.5
    return max(1, min(4, int(math.ceil(needed / 1e9))))


def _marker_path(ctx, md5):
    return os.path.join(ctx.cache_dir, md5 + ".tiling")


def _attempt_count(ctx, md5):
    """How many tiling attempts have been made for this entry (0 when unknown)."""
    try:
        with open(_marker_path(ctx, md5)) as handle:
            return int(handle.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def _attempt_due(ctx, md5):
    """True when a tiling attempt is due: never tried, window elapsed, and not given up on."""
    if _attempt_count(ctx, md5) >= TILING_MAX_ATTEMPTS:
        return False
    try:
        age = time.time() - os.path.getmtime(_marker_path(ctx, md5))
    except OSError:
        return True
    return age >= TILING_RETRY_SECONDS


def _submit_tiler(ctx, md5, name, width=None, height=None, fresh=False):
    """Submit the tiling job and record the attempt. Returns (ok, detail).

    `fresh=True` resets the attempt counter: the image itself changed, so it deserves a new budget.
    """
    attempts = 1 if fresh else _attempt_count(ctx, md5) + 1
    argv = job_argv(ctx, md5, name, width, height)
    try:
        ok, detail = ctx.submit_job(md5, argv)
    except Exception as exc:                      # noqa: BLE001
        ok, detail = False, "%s: %s" % (type(exc).__name__, exc)
    try:
        with open(_marker_path(ctx, md5), "w") as handle:
            handle.write(str(attempts))
    except OSError:
        pass
    if attempts >= TILING_MAX_ATTEMPTS and not ok:
        ctx.log("giving up on %r (md5 %s) after %d attempts: %s" % (name, md5, attempts, detail))
    elif attempts > 1:
        ctx.log("tiling attempt %d/%d for %r (md5 %s)" % (attempts, TILING_MAX_ATTEMPTS, name, md5))
    if not ok:
        # Loud on purpose: the old code discarded this and the viewer promised "preparing" forever.
        ctx.log("TILER NOT STARTED for %r (md5 %s): %s" % (name, md5, detail))
    return ok, detail


# ── the handler ──────────────────────────────────────────────────────────────────────────────────
def build_config(ctx, raw_name, polling=False):
    """Return (payload, http_status). Mirrors config.php, including its error strings."""
    if not raw_name:
        return {"error": "No file name supplied"}, 200

    name = normalize_name(raw_name)
    md5 = md5_name(name)
    jpg_abs = ctx.jpg_abs(md5)
    tile_abs = ctx.tile_abs(md5)

    meta = ctx.metadata(name)
    if not meta:
        return {"error": "Database error (found 0 results; should be 1)"}, 200
    width = int(meta["width"])
    height = int(meta.get("height") or (width // 2))

    if not polling:
        try:
            refreshed = _refresh(ctx, name, md5, width, jpg_abs, tile_abs,
                                 str(meta["timestamp"]), height)
        except Exception as exc:                      # noqa: BLE001 - never 500 the viewer
            ctx.log("refresh failed for %r: %s: %s" % (name, type(exc).__name__, exc))
            refreshed = None
        if isinstance(refreshed, dict):               # a contract-level error, e.g. not a JPEG
            return refreshed, 200

    config_path = os.path.join(tile_abs, "config.json")

    # Stuck-pending repair. A tiled panorama whose tiles were never produced -- an interrupted run, a
    # tiler that was blocked, or a cache directory cleaned up -- has a fresh preview and no config.json,
    # so no refresh happens and, before this, no job was ever submitted again. The viewer then polls
    # "a better version is being prepared" for ever. Retry on request, at most once per window.
    if not polling and width > ctx.max_width and is_full_equirectangular(width, height) \
            and not os.path.isfile(config_path) and _attempt_due(ctx, md5):
        ctx.log("RETILING %r (md5 %s): tiles missing and the last attempt is stale" % (name, md5))
        _submit_tiler(ctx, md5, name, width, height)

    if os.path.isfile(config_path):
        try:
            with open(config_path, "rb") as handle:
                config = json.loads(handle.read().decode("utf-8"))
        except Exception as exc:                      # noqa: BLE001 - corrupt cache must not 500
            ctx.log("unreadable %s: %s" % (config_path, exc))
            config = None
        if isinstance(config, dict):
            multi = config.get("multiRes")
            if not isinstance(multi, dict):
                multi = config["multiRes"] = {}
            multi["path"] = ctx.tile_rel(md5) + str(multi.get("path", "/"))
            multi["fallbackPath"] = ctx.tile_rel(md5) + str(multi.get("fallbackPath", "/"))
            config["autoLoad"] = True
            return {"pannellum": config, "width": width}, 200

    equirect = is_full_equirectangular(width, height)
    payload = {
        # reporting "pending" for a partial panorama is what made the viewer say "a better version is
        # being prepared" for ever; there is no such version and never will be.
        "multires_pending": width > ctx.max_width and equirect,
        "pannellum": {"panorama": ctx.jpg_rel(md5), "width": width, "autoLoad": True},
    }
    if not equirect:
        # only present when it applies, so the ordinary response keeps the exact shape of the PHP's
        payload["partial_panorama"] = True
    elif payload["multires_pending"] and _attempt_count(ctx, md5) >= TILING_MAX_ATTEMPTS:
        # We have given up. Saying "pending" here is a promise nothing will keep: the viewer would poll
        # for ever. Stop claiming work is in progress, and let the client say so.
        payload["multires_pending"] = False
        payload["tiling_failed"] = True
    return payload, 200


def _refresh(ctx, name, md5, width, jpg_abs, tile_abs, db_timestamp, height=None):
    """Bring the cache up to date. Returns a dict when the request should end in an error."""
    if not _needs_refresh(ctx, jpg_abs, db_timestamp):
        return None

    source = full_file_url(md5, name)
    head = ctx.fetch(source, 100)
    if not looks_like_jpeg(head):
        return {"error": "This file does not look like a valid JPEG"}

    # Invalidate the tiled version for real. PHP's rmdir() cannot do this (non-empty directory),
    # which is why a re-uploaded panorama kept serving its old tiles.
    if os.path.isdir(tile_abs):
        shutil.rmtree(tile_abs, ignore_errors=True)

    if width > ctx.max_width and is_full_equirectangular(width, height):
        preview = thumb_url(name, ctx.preview_width)
        # fresh content: this is not a retry, so the attempt budget starts again
        _submit_tiler(ctx, md5, name, width, height, fresh=True)
    else:
        preview = source

    body = ctx.fetch(preview, None)
    os.makedirs(ctx.cache_dir, exist_ok=True)
    tmp = jpg_abs + ".part"
    with open(tmp, "wb") as handle:
        handle.write(body)
    os.replace(tmp, jpg_abs)
    return None


# ── static files ─────────────────────────────────────────────────────────────────────────────────
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",   # ES modules refuse other types
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".map": "application/json; charset=utf-8",
}


def _cache_control(url_path):
    if url_path == "/" or url_path.endswith(".html"):
        return "no-cache"
    if url_path.startswith("/cache/"):
        # Tiles are keyed by md5(file name), not by content, so a re-upload can replace them.
        return "public, max-age=3600"
    return "public, max-age=300"


def resolve_static(ctx, url_path):
    """Map a URL path to (abs_path, status). 404/403 rather than a stack trace.

    The docroot is where lighttpd used to point. Two containment checks matter:

      * `public_html/src` and `public_html/examples` are SYMLINKS into ../pannellum, so the
        resolved target legitimately lies outside public_html — but it must stay inside the tool
        home. Allowing only public_html would break every asset.
      * anything resolving outside the home is refused (403), including encoded traversal.
    """
    if "\x00" in url_path:
        return None, 403

    # Never hand out a .php file as text. Under lighttpd these were EXECUTED; a static server would
    # serve the source instead -- measured on staging 2026-10-23: /health.php and /cache.php came back
    # as application/octet-stream starting with "<?php". The ported handler answers /config.php as a
    # route, so nothing legitimate needs a .php file from the docroot.
    if url_path.lower().endswith(".php"):
        return None, 404
    relative = urllib.parse.unquote(url_path).lstrip("/")
    if relative in ("", "."):
        relative = "index.html"
    candidate = os.path.join(ctx.public_dir, relative)
    real_home = os.path.realpath(ctx.home)
    real_candidate = os.path.realpath(candidate)
    if not (real_candidate == real_home or real_candidate.startswith(real_home + os.sep)):
        return None, 403
    if not os.path.isfile(real_candidate):
        return None, 404
    return real_candidate, 200


def read_static(path, size_limit=32 * 1024 * 1024):
    with open(path, "rb") as handle:
        return handle.read(size_limit)


def content_type(path):
    extension = os.path.splitext(path)[1].lower()
    return CONTENT_TYPES.get(extension) or mimetypes.guess_type(path)[0] or "application/octet-stream"


# ── production wiring ───────────────────────────────────────────────────────────────────────────
def production_context(home=None, log=None):
    """Real side effects: the wiki replica, upload.wikimedia.org, and bin/toolforge."""
    home = home or os.environ.get("HOME") or os.getcwd()
    logger = log or (lambda message: print(message, flush=True))

    db = {"connection": None}
    db_lock = threading.Lock()

    def _connection():
        """One connection per worker process, reopened if it died.

        Each uWSGI worker is a separate process, so the connection is not shared; the lock is there in
        case the service is ever run with threads (pymysql connections are not thread-safe).
        """
        import pymysql                                    # imported lazily: /healthz must not need it
        connection = db["connection"]
        if connection is not None:
            try:
                connection.ping(reconnect=True)
                return connection
            except Exception:                             # noqa: BLE001 - discard and reconnect
                try:
                    connection.close()
                except Exception:                         # noqa: BLE001
                    pass
                db["connection"] = None
        connection = pymysql.connect(
            host=os.environ.get("PANOVIEWER_DB_HOST", "commonswiki.labsdb"),
            user=os.environ.get("TOOL_REPLICA_USER"),
            password=os.environ.get("TOOL_REPLICA_PASSWORD"),
            database="commonswiki_p", connect_timeout=5, read_timeout=5,
        )
        db["connection"] = connection
        return connection

    def _load_metadata(name):
        with db_lock:
            connection = _connection()
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT img_width, img_height, img_timestamp FROM image WHERE img_name = %s",
                        (name,))
                    row = cursor.fetchone()
            except Exception:                             # noqa: BLE001
                try:
                    connection.close()
                except Exception:                         # noqa: BLE001
                    pass
                db["connection"] = None
                raise
        if not row:
            return None
        return {"width": int(row[0]), "height": int(row[1]), "timestamp": _text(row[2])}

    metadata = MetadataCache(_load_metadata)

    def fetch(url, limit=None):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read() if limit is None else response.read(limit)

    def submit_job(md5, argv):
        try:
            done = subprocess.run(argv, capture_output=True, timeout=25)
        except Exception as exc:                           # noqa: BLE001
            return False, "%s: %s" % (type(exc).__name__, exc)
        detail = (done.stdout + done.stderr).decode("utf-8", "replace").strip()[:400]
        return done.returncode == 0, "rc=%d %s" % (done.returncode, detail)

    return Context(home=home, metadata=metadata, fetch=fetch, submit_job=submit_job, log=logger)


def _text(value):
    """The wiki replicas return varbinary columns; keep JSON serialisable."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    return value

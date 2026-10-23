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
import mimetypes
import os
import shutil
import subprocess
import time
import urllib.parse
import urllib.request

MAX_WIDTH = 4000
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
                 job_image=None, tool_name=None, clock=None):
        self.home = home
        self.data_dir = data_dir or home
        self.public_dir = public_dir or os.path.join(home, "public_html")
        self.max_width = max_width
        self.cache_dir = os.path.join(self.public_dir, "cache")
        self.metadata = metadata or (lambda name: None)
        self.fetch = fetch or (lambda url, limit=None: b"")
        self.submit_job = submit_job or (lambda md5, argv: (True, "not submitted (stub)"))
        self.log = log or (lambda message: None)
        self.job_image = job_image or DEFAULT_JOB_IMAGE
        self.tool_name = tool_name or os.path.basename(home.rstrip("/")) or "panoviewer"
        self.clock = clock or (lambda: time.time())

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


# ── the tiler ────────────────────────────────────────────────────────────────────────────────────
def job_argv(ctx, md5, name):
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
    return [os.path.join(ctx.home, "bin", "toolforge"), "jobs", "run",
            "--image", ctx.job_image, "--mount=all",
            "--command", command, "pano-" + md5]


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

    if not polling:
        try:
            refreshed = _refresh(ctx, name, md5, width, jpg_abs, tile_abs,
                                 str(meta["timestamp"]))
        except Exception as exc:                      # noqa: BLE001 - never 500 the viewer
            ctx.log("refresh failed for %r: %s: %s" % (name, type(exc).__name__, exc))
            refreshed = None
        if isinstance(refreshed, dict):               # a contract-level error, e.g. not a JPEG
            return refreshed, 200

    config_path = os.path.join(tile_abs, "config.json")
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

    return {
        "multires_pending": width > ctx.max_width,
        "pannellum": {"panorama": ctx.jpg_rel(md5), "width": width, "autoLoad": True},
    }, 200


def _refresh(ctx, name, md5, width, jpg_abs, tile_abs, db_timestamp):
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

    if width > ctx.max_width:
        preview = thumb_url(name, ctx.max_width)
        argv = job_argv(ctx, md5, name)
        try:
            ok, detail = ctx.submit_job(md5, argv)
        except Exception as exc:                      # noqa: BLE001
            ok, detail = False, "%s: %s" % (type(exc).__name__, exc)
        if not ok:
            # Loud on purpose: the old code discarded this and the viewer promised "preparing"
            # forever. The preview is still served, so the page keeps working.
            ctx.log("TILER NOT STARTED for %r (md5 %s): %s" % (name, md5, detail))
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

    def metadata(name):
        import pymysql                                    # imported lazily: /healthz must not need it
        connection = pymysql.connect(
            host=os.environ.get("PANOVIEWER_DB_HOST", "commonswiki.labsdb"),
            user=os.environ.get("TOOL_REPLICA_USER"),
            password=os.environ.get("TOOL_REPLICA_PASSWORD"),
            database="commonswiki_p", connect_timeout=5, read_timeout=5,
        )
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT img_width, img_timestamp FROM image WHERE img_name = %s", (name,))
                row = cursor.fetchone()
        finally:
            connection.close()
        if not row:
            return None
        return {"width": int(row[0]), "timestamp": _text(row[1])}

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

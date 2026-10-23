"""panoviewer — WSGI staging skeleton.

Purpose right now is NOT to serve the viewer; it is to prove, inside a real
python3.11 Toolforge pod, the four things the port depends on:

  1. the runtime finds this file at $HOME/www/python/src/app.py (variable `app`),
  2. there is a usable MySQL path to the wiki replicas (a driver, or the client),
  3. the tool environment is present (HOME, TOOL_DATA_DIR, replica credentials),
  4. the docroot/blob layout the handler will read is where we think it is.

GET /healthz returns that as JSON and always 200, so the pod is not restarted in
a loop while we are learning. A production health check should instead fail
loudly (see DEPLOYED-STATE.md and PORT-panoviewer-off-php.md).

Everything here is standard library only, on purpose: if a dependency is
missing, we want `/healthz` to tell us, not to crash the import.

FOUND BY RUNNING IT (2026-10-23, first staging pod): `image.img_timestamp` is a
`varbinary(14)` column, so a Python driver hands it back as **bytes**
(b"20260813110841"), where PHP's mysqli gave a string. Unhandled, that is a
`TypeError: Object of type bytes is not JSON serializable` inside the response
and a 500 for every request -- i.e. the port would "work" in review and fail on
the first real call. Decode every value that crosses into JSON.
"""
import json
import os
import shutil
import sys
import time

DB_HOST = os.environ.get("PANOVIEWER_DB_HOST", "commonswiki.labsdb")
DB_NAME = "commonswiki_p"
# A real panorama that exists (>4000 px, so it takes the tiling path), used only
# to prove the query works end-to-end.
PROBE_FILE = "Athens-acropolis-propylaia-2023-05-panorama-2x.jpg"


def _text_value(value):
    """Rows from the wiki replicas carry varbinary columns; keep JSON happy."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    return value


def _probe_drivers():
    """Which MySQL access paths exist in this image?"""
    found = {}
    for name in ("pymysql", "MySQLdb", "mysql.connector", "mariadb"):
        try:
            mod = __import__(name)
            found[name] = getattr(mod, "__version__", "present")
        except Exception as exc:                      # noqa: BLE001 - we want the reason
            found[name] = "absent: %s" % type(exc).__name__
    return found


def _probe_db():
    """Connect with the tool's own credentials and run the real query."""
    host = DB_HOST
    user = os.environ.get("TOOL_REPLICA_USER")
    password = os.environ.get("TOOL_REPLICA_PASSWORD")
    if not (user and password):
        return {"ok": False, "error": "TOOL_REPLICA_USER/PASSWORD not in the environment"}

    started = time.time()
    try:
        import pymysql
    except Exception:
        return {
            "ok": False,
            "error": "no pymysql; see drivers above",
            "falls_back_to": "/usr/bin/mariadb: %s" % (shutil.which("mariadb") or "absent"),
        }
    try:
        conn = pymysql.connect(host=host, user=user, password=password, database=DB_NAME,
                               connect_timeout=5, read_timeout=5)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT img_width, img_height, img_timestamp FROM image WHERE img_name = %s",
                (PROBE_FILE,),
            )
            row = cur.fetchone()
        conn.close()
        return {
            "ok": bool(row),
            "host": host,
            "ms": round((time.time() - started) * 1000),
            "row": None if row is None else {
                "img_width": _text_value(row[0]),
                "img_height": _text_value(row[1]),
                "img_timestamp": _text_value(row[2]),
                "img_timestamp_type": type(_text_value(row[2])).__name__,
            },
        }
    except Exception as exc:                          # noqa: BLE001
        return {"ok": False, "host": host, "error": "%s: %s" % (type(exc).__name__, exc)}


def _probe_paths():
    home = os.environ.get("HOME", "")
    tool_data = os.environ.get("TOOL_DATA_DIR", "")
    cache = os.path.join(home, "public_html", "cache") if home else ""
    return {
        "HOME": home,
        "TOOL_DATA_DIR": tool_data,
        "docroot": {
            p: os.path.isdir(os.path.join(home, "public_html", p))
            for p in (".", "cache", "src", "examples")
        } if home else {},
        "cache_entries": (len(os.listdir(cache)) if cache and os.path.isdir(cache) else None),
        "nona": os.path.exists(os.path.join(tool_data or home, "bin", "nona")),
        "service_manifest": os.path.exists(os.path.join(home, "service.manifest")),
        "replica_my_cnf": os.path.exists(os.path.join(home, "replica.my.cnf")),
    }


def _json(start_response, payload, status="200 OK"):
    body = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    start_response(status, [("Content-Type", "application/json; charset=utf-8"),
                            ("Content-Length", str(len(body))),
                            ("Cache-Control", "no-store")])
    return [body]


def _text(start_response, text, status="200 OK", ctype="text/plain; charset=utf-8"):
    body = text.encode()
    start_response(status, [("Content-Type", ctype),
                            ("Content-Length", str(len(body))),
                            ("Cache-Control", "no-store")])
    return [body]


INDEX_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>panoviewer — staging</title>
<style>body{font-family:sans-serif;max-width:40rem;margin:3rem auto;line-height:1.5}
code{background:#eee;padding:.1rem .3rem}</style></head><body>
<h1>panoviewer — staging (panoviewer2)</h1>
<p>This is the rewrite skeleton running on <code>python3.11</code>, not the viewer.
The production tool is <a href="https://panoviewer.toolforge.org/">panoviewer.toolforge.org</a>.</p>
<p>Diagnostics: <a href="healthz">/healthz</a> — runtime, MySQL path, tool environment, docroot.</p>
</body></html>
"""


def app(environ, start_response):
    path = environ.get("PATH_INFO", "/") or "/"
    method = environ.get("REQUEST_METHOD", "GET")

    if method not in ("GET", "HEAD"):
        return _json(start_response, {"error": "method not allowed"}, status="405 Method Not Allowed")

    if path in ("/healthz", "/healthz/"):
        drivers = _probe_drivers()
        db = _probe_db()
        payload = {
            "service": "panoviewer staging skeleton",
            "python": sys.version.split()[0],
            "drivers": drivers,
            "database": db,
            "paths": _probe_paths(),
        }
        return _json(start_response, payload)

    if path in ("/", "/index.html"):
        return _text(start_response, INDEX_HTML, ctype="text/html; charset=utf-8")

    # The real endpoint the viewer calls. Not implemented here on purpose: a stub
    # that returns something plausible is worse than one that says "not yet".
    if path.rstrip("/") in ("/config.php", "/config"):
        return _json(start_response, {
            "error": "staging skeleton: the /config.php handler is not ported yet",
            "see": "php-probe/PORT-panoviewer-off-php.md",
        }, status="501 Not Implemented")

    return _json(start_response, {"error": "not found", "path": path}, status="404 Not Found")

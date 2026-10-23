"""panoviewer — WSGI entry point (Toolforge python3.11: $HOME/www/python/src/app.py -> app).

A thin shell: routing, HTTP semantics and error containment. All the behaviour lives in
`panoviewer.py`, which is why it can be tested without a database or a cluster.

Routes:
    GET /config?f=<name>[&p=1]       the viewer's data endpoint (JSON, same contract as the PHP)
    GET /config.php?f=<name>         legacy alias for the same route (see below)
    GET /healthz[?db=1]              diagnostics; cheap by default, ?db=1 also probes the replica
    GET /, /src/**, /examples/**, /cache/**    static files from public_html

Note that the docroot is served by THIS app. The python3.11 image runs only the WSGI callable —
it does not serve public_html the way lighttpd did (measured on staging 2026-10-23), so a missing
static route is a 404 for every stylesheet and tile.
"""
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import panoviewer                                            # noqa: E402

_CONTEXT = None


def context():
    global _CONTEXT
    if _CONTEXT is None:
        _CONTEXT = panoviewer.production_context()
    return _CONTEXT


def _respond(start_response, status, body, content_type="text/plain; charset=utf-8",
             cache_control="no-store", extra=None):
    if isinstance(body, str):
        body = body.encode("utf-8")
    headers = [("Content-Type", content_type),
               ("Content-Length", str(len(body))),
               ("Cache-Control", cache_control),
               ("X-Content-Type-Options", "nosniff")]
    headers.extend(extra or [])
    start_response(status, headers)
    return [_body_for_method(body)]


_METHOD_IS_HEAD = [False]


def _body_for_method(body):
    return b"" if _METHOD_IS_HEAD[0] else body


def _json_response(start_response, status, payload, cache_control="no-store"):
    body = (json.dumps(payload) + "\n").encode("utf-8")
    return _respond(start_response, status, body,
                    content_type="application/json; charset=utf-8",
                    cache_control=cache_control)


def _diagnostics(ctx, want_db):
    """Everything needed to debug a deployment, and nothing that costs a query by default."""
    checks = {
        "service": "panoviewer",
        "python": sys.version.split()[0],
        "home": ctx.home,
        "public_dir": ctx.public_dir,
        "public_dir_exists": os.path.isdir(ctx.public_dir),
        "cache_dir": ctx.cache_dir,
        "cache_entries": (len(os.listdir(ctx.cache_dir))
                          if os.path.isdir(ctx.cache_dir) else None),
        "nona": os.path.exists(os.path.join(ctx.data_dir, "bin", "nona")),
        "tiler_shim": os.path.exists(os.path.join(ctx.home, "bin", "toolforge")),
        "replica_my_cnf": os.path.exists(os.path.join(ctx.home, "replica.my.cnf")),
        "env": sorted(k for k in os.environ
                      if k.startswith("TOOL_") and not k.endswith(("PASSWORD", "SECRET"))),
    }
    try:
        import pymysql
        checks["pymysql"] = getattr(pymysql, "__version__", "present")
    except Exception as exc:                                  # noqa: BLE001
        checks["pymysql"] = "absent: %s" % type(exc).__name__
    if want_db:
        try:
            meta = ctx.metadata("Athens-acropolis-propylaia-2023-05-panorama-2x.jpg")
            checks["db_probe"] = {"ok": bool(meta), "row": meta}
        except Exception as exc:                              # noqa: BLE001
            checks["db_probe"] = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    return checks


def app(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET").upper()
    path = environ.get("PATH_INFO", "/") or "/"
    query = environ.get("QUERY_STRING", "")
    _METHOD_IS_HEAD[0] = method == "HEAD"

    if method not in ("GET", "HEAD"):
        return _respond(start_response, "405 Method Not Allowed", "method not allowed",
                        extra=[("Allow", "GET, HEAD")])

    try:
        ctx = context()
        params = {}
        for pair in query.split("&"):
            if not pair:
                continue
            key, _, value = pair.partition("=")
            params[key] = value

        if path in ("/healthz", "/healthz/"):
            return _json_response(start_response, "200 OK",
                                  _diagnostics(ctx, want_db="db" in params))

        # Canonical route is /config: there is no PHP behind it any more, and a ".php" path invites the
        # question. /config.php stays as an alias because the old index.html (and anything already cached
        # in a browser) asks for it -- verified 2026-10-23 that nothing outside this repository does:
        # 0 on-wiki references to any panoviewer path, and the Wikidata gadget links only to "/#<file>".
        if path.rstrip("/") in ("/config", "/config.php"):
            raw_name = params.get("f", "")
            if raw_name:
                raw_name = _unquote(raw_name)
            payload, status = panoviewer.build_config(
                ctx, raw_name, polling=("p" in params))
            return _json_response(start_response, "200 OK" if status == 200 else "%d" % status,
                                  payload)

        resolved, status = panoviewer.resolve_static(ctx, path)
        if status != 200:
            if status == 403:
                return _respond(start_response, "403 Forbidden", "forbidden")
            return _respond(start_response, "404 Not Found", "not found")

        body = panoviewer.read_static(resolved)
        return _respond(start_response, "200 OK", body,
                        content_type=panoviewer.content_type(resolved),
                        cache_control=panoviewer._cache_control(path))

    except Exception:                                          # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        return _json_response(start_response, "500 Internal Server Error",
                              {"error": "internal error; see the service log"})


def _unquote(value):
    from urllib.parse import unquote_plus
    return unquote_plus(value)

# `www/python` — the panoviewer port (python3.11 WSGI)

The viewer's server side used to be one PHP file (`public_html/config.php`, 153 lines). This is that
handler, ported. The PHP is still in the tree and untouched: `master` keeps the deployed state, and this
work lives on `port/python` until the acceptance tests pass.

## Layout

```
www/python/
├── src/
│   ├── app.py            WSGI entry point (Toolforge looks for exactly this path and variable `app`)
│   ├── panoviewer.py     the behaviour: naming/md5, cache freshness, JPEG sniff, tiler call,
│   │                     JSON assembly, static resolution. No I/O of its own — see Context.
│   └── requirements.txt  pymysql (installed into www/python/venv, built INSIDE a pod)
├── tests/
│   └── test_handler.py   39 fixture-based tests; no database, network or cluster
└── venv/                 built in a pod; gitignored (a bastion venv is the wrong interpreter)
```

## Run the tests

```bash
cd www/python && python3 -m unittest discover -s tests -v
```

Inside a pod (the real target interpreter):

```bash
kubectl exec <pod> -- sh -c 'cd $HOME/www/python && python3 -V && python3 -m unittest discover -s tests'
```

## Routes

| route | what it does |
|---|---|
| `GET /config.php?f=<name>` | the viewer's data endpoint; same JSON contract as the PHP |
| `GET /config.php?f=<name>&p=1` | polling: never fetches, tiles or refreshes |
| `GET /healthz[?db=1]` | diagnostics; cheap by default, `?db=1` also queries the replica |
| `GET /`, `/src/**`, `/examples/**`, `/cache/**` | static files from `public_html` |

That last row is not optional: the `python3.11` image runs **only** the WSGI callable — it does not serve
the docroot the way lighttpd did (measured on staging, see
`php-probe/PORT-panoviewer-off-php.md`). Without it, every stylesheet, script and tile is a 404.

## Three deliberate differences from the PHP

Each has a regression test, because each is a behaviour change rather than a translation.

1. **A re-upload removes the stale tiles.** `rmdir()` cannot remove a non-empty directory, so the PHP
   never invalidated a tiled version; a re-uploaded panorama kept serving its old tiles.
2. **A failed tiler submission is logged** (`TILER NOT STARTED …`) instead of being discarded with
   `exec()`'s exit status. Note the limit of this: a *successful* submission whose job later fails (for
   example, because the tiler image is denylisted) still leaves the viewer on `multires_pending`
   forever. Nothing in this design notices that; a cron or an uptime check would.
3. **varbinary columns are decoded.** `image.img_timestamp` comes back as `bytes` from a Python driver
   where `mysqli` gave a string; undecoded it is a `TypeError` inside `json.dumps`, i.e. a 500 on every
   request (found on the first staging pod).

`public_html/multires.php` is not ported: `index.html` never called it, and it had an undefined-variable
bug plus an in-request tiler run.

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
| `GET /config?f=<name>` | **canonical**: the viewer's data endpoint; same JSON contract as the PHP |
| `GET /config?f=<name>&p=1` | polling: never fetches, tiles or refreshes |
| `GET /config.php?f=<name>` | **legacy alias** for `/config` — no PHP executes on either |
| `GET /healthz[?db=1]` | diagnostics; cheap by default, `?db=1` also queries the replica |
| `GET /`, `/src/**`, `/examples/**`, `/cache/**` | static files from `public_html` |

That last row is not optional: the `python3.11` image runs **only** the WSGI callable — it does not serve
the docroot the way lighttpd did (measured on staging, see
`php-probe/PORT-panoviewer-off-php.md`). Without it, every stylesheet, script and tile is a 404.

## The tiler (`src/tile.py`)

The tiling job is Python too now, and it runs on the **stock `python3.11` image**:

```
<home>/www/python/venv/bin/python <home>/www/python/src/tile.py <cache-dir> <md5> <urlencoded name>
```

It replaced `public_html/multires.sh` and the Build Service image for one decisive reason: the registry
denylist matches this tool's whole image namespace (`Regex=^.*/tool-panoviewer/.*$`), so **no image built
for this tool can run at all**, PHP-free or not. A stock image was the only admissible runtime.

Two things it needs that are **not in git**, and must exist on the tool account:

| what | where | why |
|---|---|---|
| `nona` | `<home>/bin/nona` | Hugin's projector; 6.9 MB, sha256 `31763e23…` |
| jammy libraries | `<home>/lib/nona-jammy/` (57 files, 15 MB) | `nona` is an Ubuntu 22.04 binary; the job image is Debian 12. Missing: libIlmImf-2_5, libHalf-2_5, libjpeg.so.8, libtiff.so.5, libpng16.so.16, libexiv2.so.27, libOpenGL/libGLX/libGLU/libGLEW, libboost_filesystem 1.74, libpano13, libwebp/libjbig/libdeflate |

`tile.py` puts that directory on `LD_LIBRARY_PATH` for the `generate.py` subprocess only. Rebuild the
bundle by extracting the jammy packages from `archive.ubuntu.com` with `dpkg-deb -x` (no root needed); the
package list is in that table's "Missing" column, and `Aptfile` in this repo is the historical record.

## Pruning (`src/prune_cache.py`)

```
python3 src/prune_cache.py                 # dry run: what is rubbish, how much
python3 src/prune_cache.py --apply         # delete it
python3 src/prune_cache.py --max-bytes N   # opt in to LRU eviction to a size budget
```

It removes the tiler's `<md5>.full.jpg` temporaries and tile directories with no `config.json`, and
**never** a finished tile set or a preview unless `--max-bytes` says so. `--min-age` (default 1 h) protects
files a running job may still be using.

What it found on the live cache (2026-10-23): 7,916 temporaries = **206 GB**, plus 891 abandoned
directories = 913 MB, out of a 303 GB cache. The shell wrapper had leaked the full-resolution download for
every tiling run ever made.

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

``public_html/multires.php` is not ported: `index.html` never called it, and it had an undefined-variable
bug plus an in-request tiler run.

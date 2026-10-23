# panoviewer

A [Pannellum](https://pannellum.org/)-based 360° panorama viewer for Wikimedia Commons, used by the
[{{Pano360}}](https://commons.wikimedia.org/wiki/Template:Pano360) template — roughly **1.5 million**
Commons files link here.

**Live:** <https://panoviewer.toolforge.org/> — the file name goes in the fragment, e.g.
<https://panoviewer.toolforge.org/#Athens-acropolis-propylaia-2023-05-panorama-2x.jpg>

## How it runs

Two processes on Toolforge, both `python3.11`, no PHP anywhere:

| part | what it is |
|---|---|
| **web tier** | `www/python/src/app.py` — a WSGI app serving `/`, `/config`, the static docroot and `/cache/**` |
| **tiler** | `www/python/src/tile.py` — a Toolforge **job**: downloads a panorama, cubes it with `generate.py` + `nona`, writes the tile pyramid |
| **cache** | `public_html/cache/`, keyed by `md5(file name)`, on NFS — 200 GB budget, evicted weekly |
| **data** | the `commonswiki_p` wiki replica: one memoised lookup per panorama for its size and last upload |

A panorama wider than 4000 px is shown as a downscaled preview while the tiler builds cube faces; once
`config.json` exists the viewer switches to the multi-resolution version. Panoramas that are not 2:1
(cylindrical strips, partial panos) cannot be cubed and are served as-is.

## For maintainers

```bash
# deploy: the tool home is a checkout, so a deploy is a pull plus a restart
ssh <you>@dev.toolforge.org
sudo -niu tools.panoviewer git -C /data/project/panoviewer pull
sudo -niu tools.panoviewer toolforge webservice python3.11 restart    # reads service.template

# tests (no database, no network, no cluster)
cd www/python && python3 -m unittest discover -s tests -v            # 139 tests
```

Three scheduled jobs do the unattended work — an hourly health check that mails on failure, a nightly
tile sweep, and a weekly cache prune with a size budget. `toolforge jobs list` is the source of truth;
`www/python/README.md` documents what each one does.

## Where to look

| file | why |
|---|---|
| `www/python/README.md` | routes, the tiler, tests, scheduled jobs, and the deliberate differences from the old PHP |
| `DEPLOYED-STATE.md` | **what is not in git**: `nona` and the 13 jammy libraries it needs, `service.manifest`, the cache layout |
| `public_html/index.html` | the single page; it asks `/config` for one panorama and hands the JSON to Pannellum |

## History

Until 2026-10-23 the server side was one 153-line PHP file (`public_html/config.php`) served from a PHP
image. The PHP security lockdown blocked that image and, with it, the whole tool. The tag
`deployed-2026-10-23` is the last PHP state; everything since is Python. Why the port was not optional,
and what broke along the way, is written up in `MEMO`-style public work: see the `port/python` branch
history.

## Licence

LGPL-2.1-or-later — see [LICENSE](LICENSE). Pannellum itself lives in the `pannellum` submodule
(<https://github.com/mpetroff/pannellum>) under its own licence; `bin/nona` is Hugin's projector (GPL-2.0)
and is **not** stored in this repository — see `DEPLOYED-STATE.md`.

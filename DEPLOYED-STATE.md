# DEPLOYED-STATE — what it takes to actually run this tool

This repository is **not** the whole deployment. Read this before "restoring" anything.

| | |
|---|---|
| Tool | `panoviewer` on Toolforge → <https://panoviewer.toolforge.org/> |
| Deployed commit | tag **`deployed-2026-10-23`** = `c7992b2` + one commit (`bin/toolforge` jobs-API URL) |
| Status at that tag | **down**: the web pod's image is denylisted at admission (`php7.4`), and `php7.4` is no longer an offered webservice type |
| Full machine-captured record | `php-probe/freeze/deployed-2026-10-23.txt` (regenerate with `freeze-deployed.sh`) |

## Four parts of the deployment, only one of them in git

1. **Source** — this repository. The tool's home `/data/project/panoviewer` is a clone at
   `deployed-2026-10-23`, `git pull`-deployed as the tool user.
2. **`service.manifest`** — written by the `toolforge` CLI, **gitignored**, and the only place the runtime
   type is recorded. When captured it said:

   ```yaml
   backend: kubernetes
   health-check-path: /health.php
   version: 4
   web: php7.4
   ```

   `service.template` in this repo carries only `backend` and `health-check-path` — it has **no `type:`**.
3. **Environment variables** (values are secrets; names are all a restore needs):
   `TOOL_REPLICA_USER`, `TOOL_REPLICA_PASSWORD`, `TOOL_TOOLSDB_USER`, `TOOL_TOOLSDB_PASSWORD`.
   `~/replica.my.cnf` (mode 0400) also exists and `multires.php` reads it.
4. **`bin/nona`** — the Hugin `nona` binary that generates the cube-face tiles.

## `bin/nona` is not in git, and nothing fetches it

| | |
|---|---|
| size | 6,901,232 bytes |
| sha256 | `31763e2382817b0d7440385731aa2ad20605ba380f85a284dc82b4443c86216c` |
| placed | `2024-01-25`, hand-copied onto NFS |
| built | ELF 64-bit x86-64, dynamically linked, `BuildID[sha1]=16a13899e2f46…` |
| why | hugin-tools is absent from Ubuntu 22.04 (jammy) — see the comment in `Aptfile`; a custom binary was used instead |
| consumed by | `public_html/multires.sh`: `./generate.py --nona="$TOOL_DATA_DIR"/bin/nona …` |

**`nona` also needs 13 jammy libraries** that the stock `python3.11` job image does not have (it is Debian
12). They are bundled at `lib/nona-jammy/` — 57 files, 15 MB, extracted from `archive.ubuntu.com` with
`dpkg-deb -x`. `www/python/src/tile.py` puts that directory on `LD_LIBRARY_PATH` for `generate.py` only.

**A clone of this repository cannot tile.** Consequences:

- The binary is **deliberately not published here**: it is GPL-2.0 software from Hugin, and redistributing a
  binary without its corresponding source is a licence-compliance question, not a convenience decision.
  It lives on NFS and is checksummed above; keep a private backup if you care about it.
- The right long-term fix is not to carry a stray binary: the tiler needs only python3, PIL/numpy and
  *some* `nona`. A `python3.11` job image that installs hugin properly (or a rebuilt buildpack image)
  removes this artifact entirely.

## Pruning the cache

`www/python/src/prune_cache.py` (dry run by default) removes the tiler's `<md5>.full.jpg` temporaries and
tile directories without a `config.json`. Measured on this tool, 2026-10-23: **7,916 temporaries = 206 GB**
plus 891 abandoned directories = 913 MB — two thirds of a 303 GB cache, leaked by the old shell wrapper.

## `public_html/cache/` is data, not code

303 GB in 22,071 entries of generated tiles, gitignored. A restore must **not** migrate or repopulate it;
the PHP/ported handler recreates entries on demand. Newest tile at the time of capture:
`2026-10-20 20:16 UTC` — the last evidence the pipeline was alive before the PHP incident.

## How to run it today

It cannot run as recorded: the image `docker-registry.tools.wmflabs.org/toolforge-php74-sssd-web:latest` is
refused by the `registry-admission.tools.wmcloud.org` webhook, and `toolforge webservice --help` no longer
lists `php7.4` among its types. See `php-probe/CASE-panoviewer-2026-10.md` and
`php-probe/PORT-panoviewer-off-php.md` for the two live options (a sanctioned PHP type, or the port).

## Rolling back

There is no service to roll back to — only code. `deployed-2026-10-23` is the baseline to diff against, and
the deploy *source* is one command away from the original:

```bash
git -C /data/project/panoviewer remote set-url origin https://gitlab.wikimedia.org/toolforge-repos/panoviewer.git
```

## Staging (panoviewer2)

`panoviewer2` is the staging copy: same code, branch `port/python`, runtime `python3.11`, health check `/healthz`.
Measured there on 2026-10-23: pod `1/1 Running` ~7 s after `start`; `/healthz` reports pymysql 2.2.8 from
`$HOME/www/python/venv` and the real query against `commonswiki_p` returning `4096x2048` in ~60 ms.

Two traps found by running it, both recorded in `php-probe/PORT-panoviewer-off-php.md`:

1. **`service.template` overrides `--health-check-path` on `restart`.** Starting with `/healthz` still produced a pod
   probing `/health.php` (404, never Ready). The template is the authority, not the flag.
2. **The python3.11 image does not serve `public_html`.** uWSGI runs the WSGI app only; `/src/...` is a 404 unless the
   app serves it. The port therefore has to serve `/`, `/src/**` and `/cache/**` itself.

The venv at `www/python/venv` is gitignored: it must be created *inside a pod* (a bastion venv is the wrong interpreter).

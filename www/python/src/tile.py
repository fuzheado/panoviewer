"""Tiler entry point: generate Pannellum multires tiles for one Commons file.

Run as a Toolforge job:

    <home>/www/python/venv/bin/python <home>/www/python/src/tile.py <cache-dir> <md5> <urlencoded name>

Why this replaces `public_html/multires.sh` + the Build Service image:

  * The buildpack image was built with the **PHP** buildpack (`Procfile: heroku-php-apache2`), and the
    registry denylist blocks this tool's whole image namespace (`Regex=^.*/tool-panoviewer/.*$`), so no
    image built for this tool can run at all — PHP-free or not. The stock `python3.11` image is allowed.
  * `multires.sh` used `wget`, which the `python3.11` image does not have; it also ran `generate.py` with
    the *system* interpreter, which has no Pillow or numpy.

Two defects of the shell version are fixed here, both measured on the live cache (2026-10-23):

  1. **It never deleted the full-resolution temporary.** `<md5>.full.jpg` was written for every tiling
     run and left behind: 7,916 files, **206 GB** — two thirds of the 303 GB cache. Now removed in a
     `finally` block, so failures clean up too.
  2. **A failed run left a half-built tile directory.** 891 cache directories had no `config.json`; the
     viewer then polls `multires_pending` forever. Now a failed run removes its partial output and exits
     non-zero, so `toolforge jobs list` shows it.

Exit codes matter: the job's status is the only place a failure becomes visible.
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

USER_AGENT = "panoviewer/2.0 (https://panoviewer.toolforge.org/)"
UPLOAD_BASE = "https://upload.wikimedia.org/wikipedia/commons"


def original_url(md5, name):
    return "%s/%s/%s/%s" % (UPLOAD_BASE, md5[0], md5[:2], name)


def download(url, destination, log=print):
    """Stream to a .part file and rename, so an interrupted run leaves no fake JPEG."""
    partial = destination + ".part"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    started = time.time()
    with urllib.request.urlopen(request, timeout=180) as response:
        length = response.headers.get("Content-Length")
        with open(partial, "wb") as handle:
            shutil.copyfileobj(response, handle, 1024 * 256)
    os.replace(partial, destination)
    size = os.path.getsize(destination)
    log("downloaded %.1f MB in %.1fs%s" % (
        size / 1e6, time.time() - started,
        "" if not length else " (Content-Length %s)" % length))
    return size


def library_env(home, base=None):
    """LD_LIBRARY_PATH for nona.

    `bin/nona` is an Ubuntu 22.04 (jammy) binary; the python3.11 job image is Debian 12 (bookworm), so its
    13 sonames do not exist there: libIlmImf-2_5, libHalf-2_5, libjpeg.so.8, libtiff.so.5, libpng16.so.16,
    libexiv2.so.27, libOpenGL/libGLX/libGLU/libGLEW, libboost_filesystem 1.74, libpano13, and libtiff's
    libwebp/libjbig/libdeflate. They are bundled at <home>/lib/nona-jammy (57 files, 15 MB) and measured
    working inside the pod on 2026-10-23. Without this, nona dies with a dynamic-linker error that the
    tiler cannot otherwise explain.
    """
    env = dict(os.environ if base is None else base)
    library_dir = os.path.join(home, "lib", "nona-jammy")
    if os.path.isdir(library_dir):
        existing = env.get("LD_LIBRARY_PATH")
        env["LD_LIBRARY_PATH"] = library_dir + (":" + existing if existing else "")
    return env


def default_execute(argv, env=None):
    """Run generate.py, and keep its output: the job's own logs are not retrievable."""
    done = subprocess.run(argv, capture_output=True, env=env, timeout=3600)
    if done.returncode != 0:
        raise RuntimeError("generate.py failed (rc=%d)\n--- stdout ---\n%s\n--- stderr ---\n%s" % (
            done.returncode,
            done.stdout.decode("utf-8", "replace")[-2000:],
            done.stderr.decode("utf-8", "replace")[-2000:]))
    return done


def run(cache_dir, md5, name, home=None, fetch=download, execute=None, log=print,
        haov=None, vaov=None):
    """Generate tiles. Returns the tile directory. Raises on failure (after cleaning up)."""
    home = home or os.environ.get("TOOL_DATA_DIR") or os.environ.get("HOME") or os.getcwd()
    nona = os.path.join(home, "bin", "nona")
    generate = os.path.join(home, "public_html", "generate.py")
    env = library_env(home)
    execute = execute or (lambda argv: default_execute(argv, env))

    full = os.path.join(cache_dir, md5 + ".full.jpg")
    multi = os.path.join(cache_dir, md5)

    for path, what in ((nona, "nona binary"), (generate, "generate.py")):
        if not os.path.exists(path):
            raise RuntimeError("missing %s: %s" % (what, path))

    os.makedirs(cache_dir, exist_ok=True)
    try:
        fetch(original_url(md5, name), full, log)
        # start from scratch: a partly-written tile set is worse than none
        if os.path.isdir(multi):
            shutil.rmtree(multi, ignore_errors=True)
        log("tiling %s -> %s" % (full, multi))
        argv = [sys.executable, generate, "--nona=" + nona, "-o", multi, full]
        # generate.py only assumes 360x180 when width/height is EXACTLY 2, and exits otherwise. A
        # panorama that is 2:1 to within a rounding error is still a full panorama, so say so.
        if haov is not None and vaov is not None:
            argv += ["--haov", str(haov), "--vaov", str(vaov)]
        execute(argv)
        if not os.path.isfile(os.path.join(multi, "config.json")):
            raise RuntimeError("generate.py produced no config.json in %s" % multi)
        log("done: %s" % multi)
        return multi
    except Exception:
        # Do not leave a directory the viewer will interpret as "tiling in progress" forever.
        if os.path.isdir(multi) and not os.path.isfile(os.path.join(multi, "config.json")):
            shutil.rmtree(multi, ignore_errors=True)
            log("removed the partial tile set at %s" % multi)
        raise
    finally:
        # 206 GB of these accumulated over the years; see the module docstring.
        for leftover in (full, full + ".part"):
            try:
                os.remove(leftover)
            except FileNotFoundError:
                pass


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generate Pannellum multires tiles for one file.")
    parser.add_argument("cache_dir", help="directory holding the cache (usually <home>/public_html/cache)")
    parser.add_argument("md5", help="md5 of the normalised file name")
    parser.add_argument("name", help="URL-encoded Commons file name")
    parser.add_argument("--haov", type=float, default=None,
                        help="horizontal field of view in degrees (needed when width/height isn't exactly 2)")
    parser.add_argument("--vaov", type=float, default=None, help="vertical field of view in degrees")
    args = parser.parse_args(argv)

    name = urllib.parse.unquote(args.name)
    started = time.time()
    try:
        run(args.cache_dir, args.md5, urlencoded_name(name), log=log_line,
            haov=args.haov, vaov=args.vaov)
    except Exception as exc:                                   # noqa: BLE001
        log_line("FAILED after %.1fs: %s: %s" % (time.time() - started, type(exc).__name__, exc))
        return 1
    log_line("finished in %.1fs" % (time.time() - started))
    return 0


def urlencoded_name(name):
    return urllib.parse.quote(name, safe="")


def log_line(message):
    """stdout for the job log, plus a file we can read later.

    `toolforge jobs logs` was not available for this tool's jobs (measured 2026-10-23: "Job ... does not
    have any logs available"), so the tiler keeps its own: <TOOL_DATA_DIR>/logs/tile.log. A tiling failure
    that leaves no trace is the same class of bug as the 206 GB of temporaries.
    """
    line = "[tile %s] %s" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), message)
    print(line, flush=True)
    data_dir = os.environ.get("TOOL_DATA_DIR")
    if not data_dir:
        return                       # tests and local runs: stdout only
    try:
        path = os.path.join(data_dir, "logs", "tile.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as handle:
            handle.write(line + "\n")
    except Exception:                # noqa: BLE001 - logging must never be what fails the job
        pass


if __name__ == "__main__":
    sys.exit(main())

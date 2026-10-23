"""Fixture-based tests for the ported panoviewer handler.

Nothing here touches a database, the network or a cluster: `panoviewer.Context` takes the three
side effects as callables, so the tests pass fakes and assert on *behaviour*.

    cd www/python && python3 -m unittest discover -s tests -v

The suite is deliberately organised as "one test per promise the PHP made", because the risk of this
port is not new code — it is a quietly changed response shape.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path.insert(0, SRC)

import app as wsgi_app          # noqa: E402
import panoviewer               # noqa: E402

JPEG_HEAD = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 90
PNG_HEAD = b"\x89PNG\r\n\x1a\n" + b"\x00" * 92
SMALL_NAME = "Athens-acropolis-propylaia-2023-05-panorama-2x.jpg"
LARGE_NAME = "Big_Panorama.jpg"
NON_ASCII_NAME = "école.jpg"


def stamp(offset_seconds=0):
    return time.strftime("%Y%m%d%H%M%S", time.gmtime(time.time() + offset_seconds))


class Fixture(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="panoviewer-test-")
        self.public = os.path.join(self.home, "public_html")
        self.cache = os.path.join(self.public, "cache")
        os.makedirs(self.cache)
        # a docroot with a symlink into the submodule, as the real tree has
        os.makedirs(os.path.join(self.home, "pannellum", "src", "js"))
        with open(os.path.join(self.home, "pannellum", "src", "js", "pannellum.js"), "w") as fh:
            fh.write("// pannellum\n")
        os.symlink(os.path.join(self.home, "pannellum", "src"), os.path.join(self.public, "src"))
        with open(os.path.join(self.public, "index.html"), "w") as fh:
            fh.write("<!DOCTYPE html><title>viewer</title>")
        for name in ("multires.sh", "config.php"):
            with open(os.path.join(self.public, name), "w") as fh:
                fh.write("# placeholder\n")
        os.makedirs(os.path.join(self.home, "bin"))
        with open(os.path.join(self.home, "bin", "toolforge"), "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")

        self.rows = {}                 # name -> {"width":…, "timestamp":…}
        self.fetches = []              # [(url, limit)]
        self.jobs = []                 # [argv]
        self.job_result = (True, "rc=0")
        self.logs = []
        self.fetch_body = JPEG_HEAD * 8
        self.ctx = self.make_context()

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    # ── fixtures ────────────────────────────────────────────────────────────────────────────────
    def make_context(self, max_width=panoviewer.MAX_WIDTH):
        def metadata(name):
            return self.rows.get(name)

        def fetch(url, limit=None):
            self.fetches.append((url, limit))
            return self.fetch_body if limit is None else self.fetch_body[:limit]

        def submit_job(md5, argv):
            self.jobs.append(argv)
            return self.job_result

        return panoviewer.Context(
            home=self.home, public_dir=self.public, data_dir=self.home,
            max_width=max_width, metadata=metadata, fetch=fetch,
            submit_job=submit_job, log=self.logs.append)

    def add_row(self, name, width=2000, offset=-60):
        self.rows[name] = {"width": width, "timestamp": stamp(offset)}

    def cache_jpg(self, name):
        md5 = panoviewer.md5_name(name)
        return os.path.join(self.cache, md5 + ".jpg"), md5

    def write_tiled(self, name, mtime_offset=-60):
        """Pretend the tiler already produced tiles + config.json."""
        md5 = panoviewer.md5_name(name)
        tile_dir = os.path.join(self.cache, md5)
        os.makedirs(tile_dir, exist_ok=True)
        with open(os.path.join(tile_dir, "config.json"), "w") as fh:
            json.dump({"multiRes": {"path": "/cube/%s", "fallbackPath": "/cube/%s/fallback",
                                    "cubeMap": ["f", "r", "b", "l", "u", "d"]}}, fh)
        with open(os.path.join(tile_dir, "f.jpg"), "wb") as fh:
            fh.write(b"tile")
        jpg, _ = self.cache_jpg(name)
        with open(jpg, "wb") as fh:
            fh.write(b"old")
        when = time.time() + mtime_offset
        os.utime(jpg, (when, when))
        return tile_dir, jpg


class Naming(Fixture):
    def test_spaces_become_underscores_and_first_letter_uppercases(self):
        self.assertEqual(panoviewer.normalize_name("athens acropolis.jpg"),
                         "Athens_acropolis.jpg")

    def test_non_ascii_first_character_is_left_alone_like_php(self):
        # PHP's ucfirst() works on the first BYTE; upper-casing the character would change the md5
        # and therefore the cache directory of every file whose name is not ASCII-leading.
        self.assertEqual(panoviewer.normalize_name("école.jpg"), "école.jpg")

    def test_uppercase_first_letter_is_preserved(self):
        self.assertEqual(panoviewer.normalize_name("Already.jpg"), "Already.jpg")

    def test_md5_is_of_the_normalised_name(self):
        expected = panoviewer.md5_name("Athens_x.jpg")
        self.add_row("Athens_x.jpg")
        payload, _ = panoviewer.build_config(self.ctx, "athens x.jpg")
        self.assertEqual(payload["pannellum"]["panorama"], "cache/%s.jpg" % expected)


class SmallImage(Fixture):
    def test_flat_config_shape_matches_the_php(self):
        self.add_row(SMALL_NAME)
        payload, status = panoviewer.build_config(self.ctx, SMALL_NAME)
        self.assertEqual(status, 200)
        self.assertFalse(payload["multires_pending"])
        self.assertEqual(payload["pannellum"]["width"], 2000)
        self.assertTrue(payload["pannellum"]["autoLoad"])
        self.assertTrue(payload["pannellum"]["panorama"].startswith("cache/"))
        # the flat shape carries width inside pannellum and NO top-level width (config.php agrees)
        self.assertNotIn("width", payload)

    def test_cache_file_is_written_with_the_fetched_bytes(self):
        self.add_row(SMALL_NAME)
        path, _ = self.cache_jpg(SMALL_NAME)
        panoviewer.build_config(self.ctx, SMALL_NAME)
        self.assertTrue(os.path.isfile(path))
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), self.fetch_body)

    def test_first_fetch_is_a_100_byte_sniff_then_the_whole_file(self):
        self.add_row(SMALL_NAME)
        panoviewer.build_config(self.ctx, SMALL_NAME)
        self.assertEqual(len(self.fetches), 2)
        self.assertEqual(self.fetches[0][1], 100)
        self.assertIsNone(self.fetches[1][1])

    def test_fresh_cache_is_not_refetched(self):
        self.add_row(SMALL_NAME)
        panoviewer.build_config(self.ctx, SMALL_NAME)
        self.fetches[:] = []
        panoviewer.build_config(self.ctx, SMALL_NAME)
        self.assertEqual(self.fetches, [], "a fresh cache must not hit the network")

    def test_no_tiling_job_for_a_small_image(self):
        self.add_row(SMALL_NAME)
        panoviewer.build_config(self.ctx, SMALL_NAME)
        self.assertEqual(self.jobs, [])


class LargeImage(Fixture):
    def test_big_image_is_pending_and_served_as_a_local_thumbnail(self):
        self.add_row(LARGE_NAME, width=8000)
        payload, _ = panoviewer.build_config(self.ctx, LARGE_NAME)
        self.assertTrue(payload["multires_pending"])
        # the fetch goes to thumb.php?w=4000, but the response points at the LOCAL copy of it,
        # exactly as config.php did -- same-origin, cached on disk, no hotlinking to commons
        thumb_fetches = [u for u, _ in self.fetches if "thumb.php?w=4000" in u]
        self.assertEqual(len(thumb_fetches), 1)
        self.assertIn(urllib.parse.quote(LARGE_NAME, safe=""), thumb_fetches[0])
        self.assertEqual(payload["pannellum"]["panorama"],
                         "cache/%s.jpg" % panoviewer.md5_name(LARGE_NAME))

    def test_tiler_job_argv_runs_python_never_a_php_image(self):
        self.add_row(LARGE_NAME, width=8000)
        panoviewer.build_config(self.ctx, LARGE_NAME)
        self.assertEqual(len(self.jobs), 1)
        argv = self.jobs[0]
        md5 = panoviewer.md5_name(LARGE_NAME)
        self.assertEqual(argv[1:3], ["jobs", "run"])
        # the tool's own image namespace is denylisted (Regex ^.*/tool-panoviewer/.*$), so the tiler
        # must run on a stock image; assert we never name the blocked one again
        # the resolved reference, not the type name: the shim bypasses the CLI's name mapping
        self.assertEqual(argv[argv.index("--image") + 1],
                         "docker-registry.tools.wmflabs.org/toolforge-python311-sssd-web:latest")
        self.assertNotIn("tool-panoviewer/tool-panoviewer", " ".join(argv))
        self.assertIn("--mount=all", argv)
        # the API default is 512Mi/0.1 CPU, which OOM-kills any panorama big enough to need tiling
        self.assertEqual(argv[argv.index("--mem") + 1], "1Gi")     # 8000x4000 -> 1 GiB
        self.assertEqual(argv[argv.index("--cpu") + 1], "1")
        self.assertEqual(argv[-1], "pano-" + md5)
        command = argv[argv.index("--command") + 1]
        self.assertIn("tile.py", command)
        self.assertIn("www/python/venv/bin/python", command)
        self.assertNotIn("multires.sh", command, "the wget-based shell wrapper is gone")
        self.assertIn(self.cache, command)
        self.assertIn(md5, command)
        self.assertIn(urllib.parse.quote(LARGE_NAME, safe=""), command)

    def test_a_failed_tiler_is_loud_and_still_serves_the_preview(self):
        # the PHP discarded the exit status, so a dead tiler looked like "preparing..." forever
        self.add_row(LARGE_NAME, width=8000)
        self.job_result = (False, "rc=1 admission webhook denied")
        payload, status = panoviewer.build_config(self.ctx, LARGE_NAME)
        self.assertEqual(status, 200)
        self.assertTrue(payload["multires_pending"])
        self.assertTrue(any("TILER NOT STARTED" in line for line in self.logs),
                        "a failed tiler submission must be logged, not swallowed: %r" % (self.logs,))
        path, _ = self.cache_jpg(LARGE_NAME)
        self.assertTrue(os.path.isfile(path), "the preview must still be cached")

    def test_an_exception_from_the_tiler_does_not_500_the_viewer(self):
        self.add_row(LARGE_NAME, width=8000)

        def explode(md5, argv):
            raise OSError("boom")

        self.ctx.submit_job = explode
        payload, status = panoviewer.build_config(self.ctx, LARGE_NAME)
        self.assertEqual(status, 200)
        self.assertTrue(payload["multires_pending"])


class TiledImage(Fixture):
    def test_tiled_config_paths_are_prefixed_for_the_browser(self):
        self.add_row(LARGE_NAME, width=8000)
        self.write_tiled(LARGE_NAME)
        payload, _ = panoviewer.build_config(self.ctx, LARGE_NAME)
        md5 = panoviewer.md5_name(LARGE_NAME)
        self.assertTrue(payload["pannellum"]["autoLoad"])
        self.assertEqual(payload["pannellum"]["multiRes"]["path"], "cache/%s/cube/%%s" % md5)
        self.assertEqual(payload["pannellum"]["multiRes"]["fallbackPath"],
                         "cache/%s/cube/%%s/fallback" % md5)
        self.assertNotIn("multires_pending", payload)
        self.assertEqual(payload["width"], 8000)

    def test_corrupt_config_json_falls_back_to_the_preview(self):
        self.add_row(LARGE_NAME, width=8000)
        tile_dir, _ = self.write_tiled(LARGE_NAME)
        with open(os.path.join(tile_dir, "config.json"), "w") as fh:
            fh.write("{not json")
        payload, status = panoviewer.build_config(self.ctx, LARGE_NAME)
        self.assertEqual(status, 200)
        self.assertIn("multires_pending", payload)
        self.assertTrue(any("unreadable" in line for line in self.logs))

    def test_polling_sees_the_tiles_once_they_exist(self):
        self.add_row(LARGE_NAME, width=8000)
        self.write_tiled(LARGE_NAME)
        payload, _ = panoviewer.build_config(self.ctx, LARGE_NAME, polling=True)
        self.assertNotIn("multires_pending", payload)


class Polling(Fixture):
    def test_polling_never_fetches_or_tiles(self):
        self.add_row(LARGE_NAME, width=8000)
        payload, _ = panoviewer.build_config(self.ctx, LARGE_NAME, polling=True)
        self.assertTrue(payload["multires_pending"])
        self.assertEqual(self.fetches, [])
        self.assertEqual(self.jobs, [])

    def test_polling_reports_pending_for_a_stale_cache_without_refreshing(self):
        self.add_row(LARGE_NAME, width=8000, offset=-1)   # newer than the cache below
        jpg, _ = self.cache_jpg(LARGE_NAME)
        with open(jpg, "wb") as fh:
            fh.write(b"old")
        old = time.time() - 3600
        os.utime(jpg, (old, old))
        panoviewer.build_config(self.ctx, LARGE_NAME, polling=True)
        with open(jpg, "rb") as fh:
            self.assertEqual(fh.read(), b"old", "polling must not rewrite the cache")


class Refresh(Fixture):
    def test_a_reuploaded_file_refreshes_the_cached_jpeg(self):
        self.add_row(SMALL_NAME, offset=-30)
        jpg, _ = self.cache_jpg(SMALL_NAME)
        with open(jpg, "wb") as fh:
            fh.write(b"previous version")
        old = time.time() - 3600
        os.utime(jpg, (old, old))
        self.rows[SMALL_NAME]["timestamp"] = stamp(0)     # uploaded after the cache was written
        panoviewer.build_config(self.ctx, SMALL_NAME)
        with open(jpg, "rb") as fh:
            self.assertEqual(fh.read(), self.fetch_body)

    def test_a_reupload_actually_removes_the_stale_tiles(self):
        """Regression: the PHP called rmdir() on a NON-EMPTY directory, which cannot work."""
        self.add_row(LARGE_NAME, width=8000, offset=0)
        tile_dir, jpg = self.write_tiled(LARGE_NAME, mtime_offset=-7200)
        self.assertTrue(os.path.isdir(tile_dir))
        panoviewer.build_config(self.ctx, LARGE_NAME)
        self.assertFalse(os.path.exists(tile_dir),
                         "old tiles must be gone after a re-upload, or the viewer serves them forever")
        self.assertEqual(len(self.jobs), 1, "a refresh of a large image must resubmit the tiler")

    def test_a_fresh_large_image_does_not_resubmit_the_tiler(self):
        self.add_row(LARGE_NAME, width=8000)
        self.write_tiled(LARGE_NAME, mtime_offset=0)
        panoviewer.build_config(self.ctx, LARGE_NAME)
        self.assertEqual(self.jobs, [])


class TilerMemory(Fixture):
    """A tiling job must be given memory proportional to the panorama, or it is OOM-killed."""

    def test_small_images_get_the_floor(self):
        self.assertEqual(panoviewer.tiler_memory_gib(4096, 2048), 1)

    def test_a_26k_panorama_gets_the_ceiling(self):
        # 26000x13000 decodes to ~1.0 GB, plus six 8272px cube faces
        self.assertEqual(panoviewer.tiler_memory_gib(26000, 13000), 4)

    def test_a_14k_panorama_gets_two(self):
        self.assertEqual(panoviewer.tiler_memory_gib(14304, 7152), 2)

    def test_never_below_one_or_above_the_platform_ceiling(self):
        for width in (0, 1, 400, 4096, 26000, 100000):
            value = panoviewer.tiler_memory_gib(width, max(1, width // 2))
            self.assertGreaterEqual(value, 1, width)
            self.assertLessEqual(value, 4, width)

    def test_a_missing_height_is_derived_from_the_width(self):
        self.assertEqual(panoviewer.tiler_memory_gib(26000, None),
                         panoviewer.tiler_memory_gib(26000, 13000))

    def test_the_job_argv_carries_the_size_for_a_big_image(self):
        self.rows[LARGE_NAME] = {"width": 26000, "height": 13000, "timestamp": stamp(-60)}
        panoviewer.build_config(self.ctx, LARGE_NAME)
        argv = self.jobs[0]
        self.assertEqual(argv[argv.index("--mem") + 1], "4Gi")


class StuckPending(Fixture):
    """A preview that exists with no tiles must not poll for ever without another attempt."""

    def fresh_preview(self, name):
        jpg, md5 = self.cache_jpg(name)
        with open(jpg, "wb") as handle:
            handle.write(self.fetch_body)
        os.utime(jpg, (time.time(), time.time()))   # fresh, so no refresh will happen
        return jpg, md5

    def test_it_submits_a_job_when_tiles_are_missing_but_the_preview_is_fresh(self):
        self.add_row(LARGE_NAME, width=8000)
        _jpg, md5 = self.fresh_preview(LARGE_NAME)
        payload, _ = panoviewer.build_config(self.ctx, LARGE_NAME)
        self.assertTrue(payload["multires_pending"])
        self.assertEqual(len(self.jobs), 1, "an untitled panorama must get another attempt")
        self.assertTrue(os.path.exists(os.path.join(self.cache, md5 + ".tiling")))
        self.assertTrue(any("RETILING" in line for line in self.logs))

    def test_it_does_not_resubmit_within_the_window(self):
        self.add_row(LARGE_NAME, width=8000)
        self.fresh_preview(LARGE_NAME)
        panoviewer.build_config(self.ctx, LARGE_NAME)
        panoviewer.build_config(self.ctx, LARGE_NAME)
        panoviewer.build_config(self.ctx, LARGE_NAME)
        self.assertEqual(len(self.jobs), 1, "every page load must not queue a job")

    def test_it_resubmits_once_the_window_has_passed(self):
        self.add_row(LARGE_NAME, width=8000)
        _jpg, md5 = self.fresh_preview(LARGE_NAME)
        marker = os.path.join(self.cache, md5 + ".tiling")
        with open(marker, "w"):
            pass
        old = time.time() - panoviewer.TILING_RETRY_SECONDS - 60
        os.utime(marker, (old, old))
        panoviewer.build_config(self.ctx, LARGE_NAME)
        self.assertEqual(len(self.jobs), 1)

    def test_no_retry_when_the_tiles_are_there(self):
        self.add_row(LARGE_NAME, width=8000)
        self.write_tiled(LARGE_NAME, mtime_offset=0)
        panoviewer.build_config(self.ctx, LARGE_NAME)
        self.assertEqual(self.jobs, [])

    def test_no_retry_for_a_small_image(self):
        self.add_row(SMALL_NAME, width=2000)
        self.fresh_preview(SMALL_NAME)
        payload, _ = panoviewer.build_config(self.ctx, SMALL_NAME)
        self.assertFalse(payload["multires_pending"])
        self.assertEqual(self.jobs, [])

    def test_polling_never_queues_work(self):
        self.add_row(LARGE_NAME, width=8000)
        self.fresh_preview(LARGE_NAME)
        payload, _ = panoviewer.build_config(self.ctx, LARGE_NAME, polling=True)
        self.assertTrue(payload["multires_pending"])
        self.assertEqual(self.jobs, [])

    def test_a_successful_refresh_marks_the_attempt_so_the_retry_does_not_fire(self):
        self.add_row(LARGE_NAME, width=8000)      # no cached preview: the refresh path runs
        panoviewer.build_config(self.ctx, LARGE_NAME)
        self.assertEqual(len(self.jobs), 1, "exactly one submission, not two")


class RetryCap(Fixture):
    """A file that can never tile must stop being retried."""

    def fresh_preview(self, name):
        jpg, md5 = self.cache_jpg(name)
        with open(jpg, "wb") as handle:
            handle.write(self.fetch_body)
        now = time.time()
        os.utime(jpg, (now, now))
        return jpg, md5

    def marker(self, md5, value=None, age=None):
        path = os.path.join(self.cache, md5 + ".tiling")
        if value is not None:
            with open(path, "w") as handle:
                handle.write(str(value))
        if age is not None:
            os.utime(path, (age, age))
        return path

    def test_attempts_are_counted_in_the_marker(self):
        self.add_row(LARGE_NAME, width=8000)
        _jpg, md5 = self.fresh_preview(LARGE_NAME)
        panoviewer.build_config(self.ctx, LARGE_NAME)
        with open(os.path.join(self.cache, md5 + ".tiling")) as handle:
            self.assertEqual(handle.read().strip(), "1")

    def test_it_retries_up_to_the_limit_then_stops(self):
        self.add_row(LARGE_NAME, width=8000)
        _jpg, md5 = self.fresh_preview(LARGE_NAME)
        stale = time.time() - panoviewer.TILING_RETRY_SECONDS - 60
        for attempt in range(1, panoviewer.TILING_MAX_ATTEMPTS + 1):
            self.marker(md5, value=attempt - 1, age=stale)
            panoviewer.build_config(self.ctx, LARGE_NAME)
        self.assertEqual(len(self.jobs), panoviewer.TILING_MAX_ATTEMPTS)
        # one more window later: no further submission
        self.marker(md5, age=stale)
        panoviewer.build_config(self.ctx, LARGE_NAME)
        self.assertEqual(len(self.jobs), panoviewer.TILING_MAX_ATTEMPTS, "must give up after the cap")

    def test_a_capped_entry_still_serves_the_preview(self):
        self.add_row(LARGE_NAME, width=8000)
        _jpg, md5 = self.fresh_preview(LARGE_NAME)
        self.marker(md5, value=panoviewer.TILING_MAX_ATTEMPTS, age=time.time() - 86400)
        payload, status = panoviewer.build_config(self.ctx, LARGE_NAME)
        self.assertEqual(status, 200)
        self.assertTrue(payload["multires_pending"])
        self.assertEqual(self.jobs, [])

    def test_giving_up_is_logged_once_at_the_last_attempt(self):
        self.add_row(LARGE_NAME, width=8000)
        _jpg, md5 = self.fresh_preview(LARGE_NAME)
        self.job_result = (False, "rc=1 nona crashed")
        stale = time.time() - panoviewer.TILING_RETRY_SECONDS - 60
        self.marker(md5, value=panoviewer.TILING_MAX_ATTEMPTS - 1, age=stale)
        panoviewer.build_config(self.ctx, LARGE_NAME)
        self.assertTrue(any("giving up" in line for line in self.logs), self.logs)

    def test_a_reupload_resets_the_budget(self):
        self.add_row(LARGE_NAME, width=8000, offset=0)      # newer than the cached preview
        _jpg, md5 = self.fresh_preview(LARGE_NAME)
        old = time.time() - 7200
        os.utime(os.path.join(self.cache, md5 + ".jpg"), (old, old))   # cache now stale
        self.marker(md5, value=panoviewer.TILING_MAX_ATTEMPTS, age=old)
        panoviewer.build_config(self.ctx, LARGE_NAME)
        self.assertEqual(len(self.jobs), 1, "new content deserves a fresh attempt budget")
        with open(os.path.join(self.cache, md5 + ".tiling")) as handle:
            self.assertEqual(handle.read().strip(), "1")


class PartialPanorama(Fixture):
    """A non-2:1 image cannot be cubed; the viewer must stop promising that it will be."""

    def test_a_cylindrical_strip_is_not_pending(self):
        self.add_row("Strip_Panorama.jpg", width=14740)
        self.rows["Strip_Panorama.jpg"]["height"] = 1825
        payload, _ = panoviewer.build_config(self.ctx, "Strip_Panorama.jpg")
        self.assertFalse(payload["multires_pending"], "nothing is being prepared for a partial pano")
        self.assertTrue(payload["partial_panorama"])
        self.assertEqual(self.jobs, [], "no job may be spent on it")

    def test_it_still_serves_the_image(self):
        self.add_row("Strip_Panorama.jpg", width=14740)
        self.rows["Strip_Panorama.jpg"]["height"] = 1825
        payload, _ = panoviewer.build_config(self.ctx, "Strip_Panorama.jpg")
        self.assertEqual(payload["pannellum"]["width"], 14740)
        self.assertIn("panorama", payload["pannellum"])

    def test_a_two_to_one_image_is_still_tiled(self):
        self.rows[LARGE_NAME] = {"width": 8000, "height": 4000, "timestamp": stamp(-60)}
        payload, _ = panoviewer.build_config(self.ctx, LARGE_NAME)
        self.assertTrue(payload["multires_pending"])
        self.assertNotIn("partial_panorama", payload)
        self.assertEqual(len(self.jobs), 1)

    def test_the_ratio_test(self):
        self.assertTrue(panoviewer.is_full_equirectangular(8000, 4000))
        self.assertTrue(panoviewer.is_full_equirectangular(2048, 1024))
        self.assertFalse(panoviewer.is_full_equirectangular(14740, 1825))
        self.assertFalse(panoviewer.is_full_equirectangular(6000, 4000))
        self.assertFalse(panoviewer.is_full_equirectangular(8000, 0))


class Errors(Fixture):
    def test_missing_filename(self):
        payload, status = panoviewer.build_config(self.ctx, "")
        self.assertEqual(payload, {"error": "No file name supplied"})
        self.assertEqual(status, 200)

    def test_unknown_file_reports_the_database_error_shape(self):
        payload, status = panoviewer.build_config(self.ctx, "Nope.jpg")
        self.assertEqual(payload, {"error": "Database error (found 0 results; should be 1)"})
        self.assertEqual(status, 200)   # the JS checks for the key, not the status

    def test_non_jpeg_is_refused_and_not_cached(self):
        self.add_row(SMALL_NAME)
        self.fetch_body = PNG_HEAD * 8
        payload, status = panoviewer.build_config(self.ctx, SMALL_NAME)
        self.assertEqual(payload, {"error": "This file does not look like a valid JPEG"})
        self.assertEqual(status, 200)
        path, _ = self.cache_jpg(SMALL_NAME)
        self.assertFalse(os.path.exists(path))

    def test_bytes_from_the_driver_do_not_break_json(self):
        # img_timestamp is varbinary; the first staging pod died on exactly this
        self.add_row(SMALL_NAME)
        self.rows[SMALL_NAME]["timestamp"] = stamp(-60).encode("ascii")
        payload, status = panoviewer.build_config(self.ctx, SMALL_NAME)
        self.assertEqual(status, 200)
        json.dumps(payload)          # must not raise


class Static(Fixture):
    def test_root_maps_to_index(self):
        path, status = panoviewer.resolve_static(self.ctx, "/")
        self.assertEqual(status, 200)
        self.assertTrue(path.endswith("index.html"))

    def test_symlinked_submodule_dir_is_served(self):
        # public_html/src -> ../pannellum/src (outside the docroot, inside the home)
        path, status = panoviewer.resolve_static(self.ctx, "/src/js/pannellum.js")
        self.assertEqual(status, 200)
        self.assertTrue(path.endswith("pannellum.js"))

    def test_traversal_is_never_served(self):
        for candidate in ("/../etc/passwd", "/%2e%2e/%2e%2e/etc/passwd", "/../../etc/passwd",
                          "/../../../../etc/hosts"):
            path, status = panoviewer.resolve_static(self.ctx, candidate)
            self.assertIsNone(path, "served something for %s" % candidate)
            self.assertIn(status, (403, 404), candidate)

    def test_escaping_the_home_is_forbidden_rather_than_not_found(self):
        path, status = panoviewer.resolve_static(self.ctx, "/../../etc/passwd")
        self.assertEqual((path, status), (None, 403))

    def test_missing_file_is_404(self):
        self.assertEqual(panoviewer.resolve_static(self.ctx, "/src/nope.js"), (None, 404))

    def test_tiles_and_previews_are_served_from_the_cache(self):
        jpg, md5 = self.cache_jpg(SMALL_NAME)
        with open(jpg, "wb") as fh:
            fh.write(JPEG_HEAD)
        path, status = panoviewer.resolve_static(self.ctx, "/cache/%s.jpg" % md5)
        self.assertEqual(status, 200)

    def test_php_files_are_never_served_as_source(self):
        # under lighttpd .php was executed; a static route would leak the source (measured on staging)
        for name in ("health.php", "cache.php"):
            with open(os.path.join(self.public, name), "w") as handle:
                handle.write("<?php echo 'secret'; ?>")
            self.assertEqual(panoviewer.resolve_static(self.ctx, "/" + name), (None, 404), name)

    def test_javascript_gets_a_javascript_content_type(self):
        self.assertEqual(panoviewer.content_type("x.js"), "text/javascript; charset=utf-8")

    def test_html_is_not_cached_but_tiles_are(self):
        self.assertEqual(panoviewer._cache_control("/index.html"), "no-cache")
        self.assertIn("max-age", panoviewer._cache_control("/cache/abc.jpg"))


class Wsgi(Fixture):
    def call(self, path, query=""):
        captured = {}

        def start_response(status, headers):
            captured["status"] = status
            captured["headers"] = dict(headers)

        environ = {"REQUEST_METHOD": "GET", "PATH_INFO": path, "QUERY_STRING": query}
        body = b"".join(wsgi_app.app(environ, start_response))
        return captured["status"], captured["headers"], body

    def setUp(self):
        Fixture.setUp(self)
        wsgi_app._CONTEXT = self.ctx          # inject the fixture-backed context

    def tearDown(self):
        wsgi_app._CONTEXT = None
        Fixture.tearDown(self)

    def test_config_endpoint_returns_json(self):
        self.add_row(SMALL_NAME)
        status, headers, body = self.call("/config.php", "f=" + urllib.parse.quote(SMALL_NAME))
        self.assertEqual(status, "200 OK")
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(headers["Cache-Control"], "no-store")
        payload = json.loads(body)
        self.assertTrue(payload["pannellum"]["autoLoad"])

    def test_url_encoded_space_in_the_filename(self):
        self.add_row("Athens_x.jpg")
        status, _, body = self.call("/config.php", "f=athens%20x.jpg")
        self.assertEqual(status, "200 OK")
        self.assertEqual(json.loads(body)["pannellum"]["width"], 2000)

    def test_polling_flag(self):
        self.add_row(LARGE_NAME, width=8000)
        _, _, body = self.call("/config.php", "f=%s&p=1" % urllib.parse.quote(LARGE_NAME))
        self.assertTrue(json.loads(body)["multires_pending"])
        self.assertEqual(self.fetches, [])

    def test_healthz_is_cheap_and_reports_the_runtime(self):
        status, headers, body = self.call("/healthz")
        self.assertEqual(status, "200 OK")
        checks = json.loads(body)
        self.assertIn("python", checks)
        self.assertTrue(checks["public_dir_exists"])
        self.assertNotIn("db_probe", checks, "the health check must not query by default")

    def test_index_is_served(self):
        status, headers, body = self.call("/")
        self.assertEqual(status, "200 OK")
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn(b"viewer", body)

    def test_the_config_route_is_not_a_static_file(self):
        # /config.php is answered by the handler, so blocking .php statically must not shadow it
        status, _headers, body = self.call("/config.php", "f=x.jpg")
        self.assertEqual(status, "200 OK")
        self.assertIn(b"error", body)

    def test_the_canonical_route_is_config(self):
        self.add_row(SMALL_NAME)
        status, _, body = self.call("/config", "f=" + urllib.parse.quote(SMALL_NAME))
        self.assertEqual(status, "200 OK")
        self.assertTrue(json.loads(body)["pannellum"]["autoLoad"])

    def test_the_legacy_php_route_still_answers(self):
        # a browser holding the old index.html, or any caller I cannot see, must keep working
        self.add_row(SMALL_NAME)
        status, _, body = self.call("/config.php", "f=" + urllib.parse.quote(SMALL_NAME))
        self.assertEqual(status, "200 OK")
        self.assertEqual(json.loads(body)["pannellum"]["width"], 2000)

    def test_index_html_asks_for_the_canonical_route(self):
        page = os.path.join(os.path.dirname(HERE), "..", "..", "public_html", "index.html")
        with open(page, encoding="utf-8") as handle:
            html = handle.read()
        self.assertNotIn("config.php", html,
                         "index.html must ask for /config; the .php alias is only for old callers")

    def test_post_is_method_not_allowed(self):
        captured = {}

        def start_response(status, headers):
            captured["status"] = status

        wsgi_app.app({"REQUEST_METHOD": "POST", "PATH_INFO": "/config.php",
                      "QUERY_STRING": "f=x.jpg"}, start_response)
        self.assertEqual(captured["status"], "405 Method Not Allowed")


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Tests for the tiler entry point (tile.py) — the job that runs on the python3.11 image.

The two things worth testing here are not "does it tile" (that is generate.py's job, exercised by the
staging run) but **what it leaves behind**: a 206 GB pile of `.full.jpg` temporaries and 891 abandoned
tile directories came from this code path never cleaning up.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

import tile                                                    # noqa: E402

NAME = "Big Panorama.jpg"
MD5 = "0123456789abcdef0123456789abcdef"


class TilerFixture(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="panoviewer-tile-")
        self.cache = os.path.join(self.home, "public_html", "cache")
        os.makedirs(self.cache)
        os.makedirs(os.path.join(self.home, "bin"))
        for path in (os.path.join(self.home, "bin", "nona"),
                     os.path.join(self.home, "public_html", "generate.py")):
            with open(path, "w") as fh:
                fh.write("#!/bin/sh\n")
        self.fetched = []
        self.executed = []
        self.logs = []
        self.produce_config = True

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def fetch(self, url, destination, log=None):
        self.fetched.append(url)
        with open(destination, "wb") as fh:
            fh.write(b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 500)
        return 500

    def execute(self, argv):
        self.executed.append(argv)
        if self.produce_config:
            out = argv[argv.index("-o") + 1]
            os.makedirs(out, exist_ok=True)
            with open(os.path.join(out, "config.json"), "w") as fh:
                json.dump({"multiRes": {"path": "/cube/%s"}}, fh)

    def run_tiler(self, **overrides):
        kwargs = dict(home=self.home, fetch=self.fetch, execute=self.execute,
                      log=self.logs.append)
        kwargs.update(overrides)
        return tile.run(self.cache, MD5, tile.urlencoded_name(NAME), **kwargs)


class Download(TilerFixture):
    def test_url_is_the_md5_sharded_upload_path(self):
        url = tile.original_url(MD5, tile.urlencoded_name(NAME))
        self.assertTrue(url.startswith("https://upload.wikimedia.org/wikipedia/commons/0/01/"))
        self.assertIn("Big%20Panorama.jpg", url)

    def test_it_fetches_then_runs_generate_with_nona(self):
        self.run_tiler()
        self.assertEqual(len(self.fetched), 1)
        argv = self.executed[0]
        self.assertIn("--nona=" + os.path.join(self.home, "bin", "nona"), argv)
        self.assertEqual(argv[-1], os.path.join(self.cache, MD5 + ".full.jpg"))
        self.assertEqual(argv[argv.index("-o") + 1], os.path.join(self.cache, MD5))


class Cleanup(TilerFixture):
    """The leak that cost 206 GB."""

    def test_the_full_resolution_temporary_is_deleted_on_success(self):
        self.run_tiler()
        leftover = os.path.join(self.cache, MD5 + ".full.jpg")
        self.assertFalse(os.path.exists(leftover),
                         "the .full.jpg must not survive a successful run (206 GB on the live cache)")
        self.assertEqual([n for n in os.listdir(self.cache) if n.endswith(".full.jpg")], [])

    def test_the_temporary_is_deleted_even_when_generate_fails(self):
        def explode(argv):
            raise RuntimeError("generate.py blew up")

        with self.assertRaises(RuntimeError):
            self.run_tiler(execute=explode)
        self.assertFalse(os.path.exists(os.path.join(self.cache, MD5 + ".full.jpg")))

    def test_a_failed_run_removes_its_partial_tile_directory(self):
        # 891 cache directories on the live tool had no config.json: the viewer polls "pending" forever
        def half_done(argv):
            out = argv[argv.index("-o") + 1]
            os.makedirs(out, exist_ok=True)
            with open(os.path.join(out, "f.jpg"), "wb") as fh:
                fh.write(b"half a cube face")
            raise RuntimeError("nona died")

        with self.assertRaises(RuntimeError):
            self.run_tiler(execute=half_done)
        self.assertFalse(os.path.exists(os.path.join(self.cache, MD5)),
                         "a partial tile set must not be left for the viewer to interpret")

    def test_a_successful_run_keeps_a_previously_broken_directory_out_of_the_way(self):
        stale = os.path.join(self.cache, MD5)
        os.makedirs(stale)
        with open(os.path.join(stale, "old-tile.jpg"), "wb") as fh:
            fh.write(b"stale")

        def check_clean(argv):
            # the directory must be gone before generate.py starts, or old tiles mix with new ones
            self.assertFalse(os.path.exists(stale), "the old tile directory must be removed first")
            self.execute(argv)

        self.run_tiler(execute=check_clean)
        self.assertTrue(os.path.isfile(os.path.join(self.cache, MD5, "config.json")))


class LibraryPath(TilerFixture):
    def test_the_bundled_jammy_libraries_are_put_on_the_path(self):
        os.makedirs(os.path.join(self.home, "lib", "nona-jammy"))
        env = tile.library_env(self.home, base={"PATH": "/bin"})
        self.assertEqual(env["LD_LIBRARY_PATH"], os.path.join(self.home, "lib", "nona-jammy"))

    def test_an_existing_path_is_kept_after_the_bundle(self):
        os.makedirs(os.path.join(self.home, "lib", "nona-jammy"))
        env = tile.library_env(self.home, base={"LD_LIBRARY_PATH": "/usr/lib"})
        self.assertTrue(env["LD_LIBRARY_PATH"].endswith(":/usr/lib"))

    def test_no_bundle_means_no_override(self):
        env = tile.library_env(self.home, base={"PATH": "/bin"})
        self.assertNotIn("LD_LIBRARY_PATH", env)

    def test_generate_failure_output_is_captured(self):
        with self.assertRaises(RuntimeError) as caught:
            tile.default_execute([sys.executable, "-c", "import sys; sys.stderr.write('nona said no'); sys.exit(3)"])
        self.assertIn("rc=3", str(caught.exception))
        self.assertIn("nona said no", str(caught.exception))


class Validation(TilerFixture):
    def test_missing_nona_is_an_error(self):
        os.remove(os.path.join(self.home, "bin", "nona"))
        with self.assertRaises(RuntimeError) as caught:
            self.run_tiler()
        self.assertIn("nona", str(caught.exception))
        self.assertEqual(self.fetched, [], "nothing should be downloaded if the tiler cannot run")

    def test_generate_that_produces_no_config_is_a_failure(self):
        self.produce_config = False
        with self.assertRaises(RuntimeError) as caught:
            self.run_tiler()
        self.assertIn("config.json", str(caught.exception))

    def test_no_partial_file_is_left_by_the_downloader(self):
        self.run_tiler()
        self.assertEqual([n for n in os.listdir(self.cache) if n.endswith(".part")], [])


class EntryPoint(TilerFixture):
    def test_main_returns_zero_on_success(self):
        original = tile.run
        tile.run = lambda *a, **k: os.path.join(self.cache, MD5)   # noqa: ARG005
        try:
            code = tile.main([self.cache, MD5, tile.urlencoded_name(NAME)])
        finally:
            tile.run = original
        self.assertEqual(code, 0)

    def test_main_returns_non_zero_on_failure(self):
        original = tile.run

        def boom(*_args, **_kwargs):
            raise RuntimeError("no")

        tile.run = boom
        try:
            code = tile.main([self.cache, MD5, tile.urlencoded_name(NAME)])
        finally:
            tile.run = original
        self.assertEqual(code, 1, "a failed job must exit non-zero, or it is invisible in jobs list")


if __name__ == "__main__":
    unittest.main(verbosity=2)

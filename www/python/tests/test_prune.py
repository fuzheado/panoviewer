"""Tests for prune_cache.py — the tool that removes the 206 GB of tiler temporaries.

These assert the *safety* properties more than the deletion, because the failure mode that matters is
deleting something a running job needs, or a finished tile set.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

import prune_cache                                            # noqa: E402

OLD = time.time() - 7200      # older than the default 1 h threshold
NEW = time.time() - 5


class PruneFixture(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.mkdtemp(prefix="panoviewer-prune-")

    def tearDown(self):
        shutil.rmtree(self.cache, ignore_errors=True)

    def make_file(self, name, size=1000, age=OLD):
        path = os.path.join(self.cache, name)
        with open(path, "wb") as handle:
            handle.write(b"x" * size)
        os.utime(path, (age, age))
        return path

    def make_tiles(self, name, finished=True, age=OLD):
        directory = os.path.join(self.cache, name)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "f0_0.jpg"), "wb") as handle:
            handle.write(b"t" * 500)
        if finished:
            with open(os.path.join(directory, "config.json"), "w") as handle:
                json.dump({"multiRes": {}}, handle)
        os.utime(directory, (age, age))
        return directory


class Temporaries(PruneFixture):
    def test_a_full_resolution_temporary_is_a_candidate(self):
        self.make_file("abc.full.jpg", size=5_000_000)
        report = prune_cache.scan(self.cache)
        self.assertEqual(len(report["temporaries"]), 1)
        self.assertEqual(prune_cache._total(report)[1], 5_000_000)

    def test_a_recent_temporary_is_protected(self):
        # a tiling job that started seconds ago must keep its input
        self.make_file("abc.full.jpg", age=NEW)
        report = prune_cache.scan(self.cache)
        self.assertEqual(report["temporaries"], [])
        self.assertEqual(report["kept"]["too_recent"], 1)

    def test_a_half_written_download_is_also_a_candidate(self):
        self.make_file("abc.full.jpg.part")
        report = prune_cache.scan(self.cache)
        self.assertEqual(len(report["temporaries"]), 1)

    def test_apply_removes_it(self):
        path = self.make_file("abc.full.jpg")
        report = prune_cache.scan(self.cache)
        removed, freed = prune_cache.apply(report, log=lambda _m: None)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(removed, 1)
        self.assertEqual(freed, 1000)


class AbandonedTiles(PruneFixture):
    def test_a_tile_dir_without_config_json_is_a_candidate(self):
        self.make_tiles("dead", finished=False)
        report = prune_cache.scan(self.cache)
        self.assertEqual(len(report["abandoned_tiles"]), 1)

    def test_a_finished_tile_dir_is_never_a_candidate(self):
        self.make_tiles("done", finished=True, age=OLD)
        report = prune_cache.scan(self.cache)
        self.assertEqual(report["abandoned_tiles"], [])
        self.assertEqual(report["kept"]["complete_tiles"], 1)

    def test_a_recent_abandoned_dir_is_protected(self):
        self.make_tiles("just-started", finished=False, age=NEW)
        report = prune_cache.scan(self.cache)
        self.assertEqual(report["abandoned_tiles"], [])

    def test_apply_removes_only_the_abandoned_one(self):
        dead = self.make_tiles("dead", finished=False)
        alive = self.make_tiles("done", finished=True)
        report = prune_cache.scan(self.cache)
        prune_cache.apply(report, log=lambda _m: None)
        self.assertFalse(os.path.exists(dead))
        self.assertTrue(os.path.exists(alive))


class Previews(PruneFixture):
    def test_flat_previews_survive_by_default(self):
        self.make_file("abc.jpg", size=2_000_000)
        report = prune_cache.scan(self.cache)
        self.assertEqual(prune_cache._total(report)[0], 0)
        self.assertEqual(report["kept"]["flat_jpg"], 1)

    def test_lru_eviction_is_opt_in_and_oldest_first(self):
        oldest = self.make_file("oldest.jpg", size=1_000_000, age=OLD - 100)
        newer = self.make_file("newer.jpg", size=1_000_000, age=OLD)
        report = prune_cache.scan(self.cache, max_bytes=1_000_000)
        self.assertEqual([path for path, _s in report["lru"]], [oldest])
        self.assertNotIn(newer, [path for path, _s in report["lru"]])


class Modes(PruneFixture):
    def test_dry_run_deletes_nothing(self):
        path = self.make_file("abc.full.jpg")
        exit_code = prune_cache.main(["--cache", self.cache])
        self.assertEqual(exit_code, 0)
        self.assertTrue(os.path.exists(path), "a dry run must not delete anything")

    def test_apply_deletes(self):
        path = self.make_file("abc.full.jpg")
        exit_code = prune_cache.main(["--cache", self.cache, "--apply"])
        self.assertEqual(exit_code, 0)
        self.assertFalse(os.path.exists(path))

    def test_json_report_is_machine_readable(self):
        self.make_file("abc.full.jpg", size=1234)
        import io
        import contextlib
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            prune_cache.main(["--cache", self.cache, "--json"])
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["mode"], "dry-run")
        self.assertEqual(payload["temporaries"], 1)
        self.assertEqual(payload["bytes"], 1234)

    def test_a_missing_cache_dir_is_not_an_error(self):
        report = prune_cache.scan(os.path.join(self.cache, "nope"))
        self.assertEqual(prune_cache._total(report), (0, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)

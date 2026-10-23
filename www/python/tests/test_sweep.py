"""Tests for the batch sweep (sweep_tiler.py).

The sweep's job is to queue work for panoramas *nobody has looked at*, so the tests are about the
decisions it must not get wrong: never re-queue something already tiled, never resurrect an entry the
handler gave up on, never tile a file that was never tiled by design, and never submit anything in a
dry run.
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

import panoviewer                                                # noqa: E402
import sweep_tiler                                              # noqa: E402

TILED = "Already Tiled Panorama.jpg"
UNTILED = "Big Untiled Panorama.jpg"
SMALL = "Small Panorama.jpg"
CAPPED = "Hopeless Panorama.jpg"


def page(title, width, height=None):
    return {"title": "File:" + title,
            "imageinfo": [{"width": width, "height": height or width // 2}]}


class SweepFixture(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.mkdtemp(prefix="panoviewer-sweep-")

    def tearDown(self):
        shutil.rmtree(self.cache, ignore_errors=True)

    def make_tiled(self, name):
        directory = os.path.join(self.cache, panoviewer.md5_name(panoviewer.normalize_name(name)))
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "config.json"), "w") as handle:
            json.dump({"multiRes": {}}, handle)

    def make_preview(self, name):
        md5 = panoviewer.md5_name(panoviewer.normalize_name(name))
        with open(os.path.join(self.cache, md5 + ".jpg"), "wb") as handle:
            handle.write(b"\xff\xd8\xff\xe0\x00\x10JFIF")

    def make_capped(self, name):
        md5 = panoviewer.md5_name(panoviewer.normalize_name(name))
        with open(os.path.join(self.cache, md5 + ".tiling"), "w") as handle:
            handle.write(str(panoviewer.TILING_MAX_ATTEMPTS))


class Decisions(SweepFixture):
    def test_already_tiled_is_skipped(self):
        self.make_tiled(TILED)
        result = sweep_tiler.plan(self.cache, [page(TILED, 8000)])
        self.assertEqual(result["tiled"], 1)
        self.assertEqual(result["queue"], [])

    def test_a_large_untiled_panorama_is_queued(self):
        self.make_preview(UNTILED)          # the tool has served it, so it can look hazy
        result = sweep_tiler.plan(self.cache, [page(UNTILED, 26000, 13000)])
        self.assertEqual(len(result["queue"]), 1)
        item = result["queue"][0]
        self.assertEqual(item["md5"], panoviewer.md5_name("Big_Untiled_Panorama.jpg"))
        self.assertEqual((item["width"], item["height"]), (26000, 13000))

    def test_a_small_panorama_is_not_queued(self):
        # <= MAX_WIDTH is served as a single image by design; tiling it would be pointless
        self.make_preview(SMALL)
        result = sweep_tiler.plan(self.cache, [page(SMALL, 3000, 1500)])
        self.assertEqual(result["too_small"], 1)
        self.assertEqual(result["queue"], [])

    def test_a_file_nobody_has_opened_is_not_pre_generated(self):
        # tiling the whole category would grow the cache to tens of GB for files nobody has viewed
        result = sweep_tiler.plan(self.cache, [page(UNTILED, 26000, 13000)])
        self.assertEqual(result["not_cached"], 1)
        self.assertEqual(result["queue"], [])
        # ... unless explicitly asked
        result = sweep_tiler.plan(self.cache, [page(UNTILED, 26000, 13000)], include_unseen=True)
        self.assertEqual(len(result["queue"]), 1)

    def test_an_entry_that_reached_the_attempt_cap_is_not_resurrected(self):
        self.make_preview(CAPPED)
        self.make_capped(CAPPED)
        result = sweep_tiler.plan(self.cache, [page(CAPPED, 26000, 13000)])
        self.assertEqual(result["capped"], 1)
        self.assertEqual(result["queue"], [])

    def test_a_file_without_size_info_is_counted_not_guessed(self):
        result = sweep_tiler.plan(self.cache, [{"title": "File:No Info.jpg", "imageinfo": [{}]}])
        self.assertEqual(result["no_info"], 1)
        self.assertEqual(result["queue"], [])

    def test_the_title_is_normalised_like_the_handler_does(self):
        self.make_preview("lower case name.jpg")
        result = sweep_tiler.plan(self.cache, [page("lower case name.jpg", 9000, 4500)])
        self.assertEqual(result["queue"][0]["name"], "Lower_case_name.jpg")


class Submission(SweepFixture):
    def test_it_queues_with_memory_sized_for_the_image(self):
        calls = []
        self.make_preview(UNTILED)
        items = sweep_tiler.plan(self.cache, [page(UNTILED, 26000, 13000)])["queue"]
        submitted, failed = sweep_tiler.submit(
            items, home=self.cache, cache_dir=self.cache,
            run=lambda argv: calls.append(argv) or type("R", (), {"returncode": 0})())
        self.assertEqual((submitted, failed), (1, 0))
        argv = calls[0]
        self.assertEqual(argv[argv.index("--mem") + 1], "4Gi")
        self.assertEqual(argv[-1], "pano-" + items[0]["md5"])

    def test_submitting_records_the_attempt_so_the_next_run_skips_it(self):
        self.make_preview(UNTILED)
        items = sweep_tiler.plan(self.cache, [page(UNTILED, 26000, 13000)])["queue"]
        sweep_tiler.submit(items, home=self.cache, cache_dir=self.cache,
                           run=lambda argv: type("R", (), {"returncode": 0})())
        again = sweep_tiler.plan(self.cache, [page(UNTILED, 26000, 13000)])
        self.assertEqual(len(again["queue"]), 1, "still untitled, so still a candidate")
        # still untitled, but the marker now exists; the handler's window logic decides from here
        self.assertEqual(panoviewer._attempt_count(type("C", (), {"cache_dir": self.cache})(),
                                                   items[0]["md5"]), 1)

    def test_a_failed_submission_is_counted_and_reported(self):
        logs = []
        self.make_preview(UNTILED)
        items = sweep_tiler.plan(self.cache, [page(UNTILED, 26000, 13000)])["queue"]
        submitted, failed = sweep_tiler.submit(
            items, home=self.cache, cache_dir=self.cache, log=logs.append,
            run=lambda argv: type("R", (), {"returncode": 1, "stderr": b"admission denied"})())
        self.assertEqual((submitted, failed), (0, 1))
        self.assertTrue(any("admission denied" in line for line in logs))


class Modes(SweepFixture):
    def setUp(self):
        SweepFixture.setUp(self)
        self.make_preview(UNTILED)          # served once, so it is a repair candidate
        self.pages = [page(UNTILED, 26000, 13000)]

    def stub_api(self):
        calls = []

        def fake_fetch(category, continuation=None, limit=50, interval=1.0):
            calls.append(category)
            return ([] if calls[1:] else self.pages), None

        sweep_tiler.fetch_page = fake_fetch
        return calls

    def test_a_dry_run_queues_nothing(self):
        submitted = []
        original = sweep_tiler.submit
        sweep_tiler.submit = lambda *a, **k: submitted.append(a) or (0, 0)
        try:
            self.stub_api()
            sweep_tiler.main(["--cache", self.cache, "--limit", "50"])
        finally:
            sweep_tiler.submit = original
        self.assertEqual(submitted, [], "a dry run must not submit")

    def test_apply_queues_one_job(self):
        submitted = []
        original = sweep_tiler.submit
        sweep_tiler.submit = lambda items, *a, **k: submitted.append(items) or (len(items), 0)
        try:
            self.stub_api()
            sweep_tiler.main(["--cache", self.cache, "--limit", "50", "--apply"])
        finally:
            sweep_tiler.submit = original
        self.assertEqual(len(submitted[0]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

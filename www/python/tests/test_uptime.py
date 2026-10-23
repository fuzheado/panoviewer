"""Tests for the hourly health check (check_uptime.py) and the metadata cache.

The health check's own failure modes matter as much as the tool's: a check that cannot report a problem
is worse than no check, because it also removes the suspicion. So every branch here asserts a specific
message, and the healthy case asserts exit 0.
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

import check_uptime                                              # noqa: E402
import panoviewer                                                # noqa: E402

HEALTH_OK = {"python": "3.11.2", "public_dir_exists": True, "db_probe": {"ok": True}}
TILED = {"pannellum": {"multiRes": {"path": "cache/x/%l/%s%y_%x"}, "autoLoad": True}, "width": 8000}


def fake_get(responses):
    """responses: list of (status, ctype, body) consumed in order."""
    calls = []

    def get(url, timeout=30):
        calls.append(url)
        return responses[len(calls) - 1]

    return get, calls


class Healthy(unittest.TestCase):
    def test_a_healthy_tool_exits_zero(self):
        get, calls = fake_get([
            (200, "application/json", json.dumps(HEALTH_OK).encode()),
            (200, "application/json", json.dumps(TILED).encode()),
            (200, "image/jpeg", b"\xff\xd8\xff..."),
        ])
        ok, message = check_uptime.run(get=get)
        self.assertTrue(ok, message)
        self.assertIn("tiles serving", message)
        self.assertEqual(len(calls), 3, "health, config, tile")

    def test_main_returns_zero(self):
        original = check_uptime.run
        check_uptime.run = lambda **kwargs: (True, "ok")
        try:
            self.assertEqual(check_uptime.main([]), 0)
        finally:
            check_uptime.run = original


class Failures(unittest.TestCase):
    def check(self, responses):
        get, _calls = fake_get(responses)
        return check_uptime.run(get=get)

    def test_a_5xx_from_healthz_is_a_failure(self):
        ok, message = self.check([(503, "text/plain", b"no healthy upstream")])
        self.assertFalse(ok)
        self.assertIn("503", message)

    def test_a_missing_docroot_is_a_failure(self):
        body = dict(HEALTH_OK, public_dir_exists=False)
        ok, message = self.check([(200, "application/json", json.dumps(body).encode())])
        self.assertFalse(ok)
        self.assertIn("docroot", message)

    def test_an_unreachable_database_is_a_failure(self):
        # the check must notice a broken replica, not just a served file
        body = dict(HEALTH_OK, db_probe={"ok": False, "error": "OperationalError: gone"})
        ok, message = self.check([(200, "application/json", json.dumps(body).encode())])
        self.assertFalse(ok)
        self.assertIn("database probe failed", message)
        self.assertIn("gone", message)

    def test_an_untiled_panorama_is_a_failure(self):
        ok, message = self.check([
            (200, "application/json", json.dumps(HEALTH_OK).encode()),
            (200, "application/json", json.dumps({"multires_pending": True}).encode()),
        ])
        self.assertFalse(ok)
        self.assertIn("not tiled", message)

    def test_a_missing_tile_is_a_failure(self):
        ok, message = self.check([
            (200, "application/json", json.dumps(HEALTH_OK).encode()),
            (200, "application/json", json.dumps(TILED).encode()),
            (404, "text/plain", b"not found"),
        ])
        self.assertFalse(ok)
        self.assertIn("tile answered HTTP 404", message)

    def test_a_tile_served_as_text_is_a_failure(self):
        ok, message = self.check([
            (200, "application/json", json.dumps(HEALTH_OK).encode()),
            (200, "application/json", json.dumps(TILED).encode()),
            (200, "text/html", b"<html>error page</html>"),
        ])
        self.assertFalse(ok)
        self.assertIn("not an image", message)

    def test_main_returns_one_and_says_why(self):
        original = check_uptime.run
        check_uptime.run = lambda **kwargs: (False, "the reason")
        try:
            self.assertEqual(check_uptime.main([]), 1)
        finally:
            check_uptime.run = original


class ContextWiring(unittest.TestCase):
    """A regression test for the bug the hourly check caught on its first run."""

    def test_an_empty_cache_is_still_used(self):
        # MetadataCache used to define __len__, so an empty one was FALSY and `metadata or stub`
        # replaced it with "always None" -- every /config then reported a database error.
        cache = panoviewer.MetadataCache(lambda n: {"width": 8000, "height": 4000, "timestamp": "x"})
        self.assertEqual(cache.size(), 0)
        ctx = panoviewer.Context(home="/tmp", metadata=cache)
        self.assertIs(ctx.metadata, cache)
        self.assertEqual(ctx.metadata("anything")["width"], 8000)

    def test_the_other_injected_callables_survive_too(self):
        fetch = lambda url, limit=None: b""            # noqa: E731
        ctx = panoviewer.Context(home="/tmp", fetch=fetch)
        self.assertIs(ctx.fetch, fetch)


class Trend(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.cache = tempfile.mkdtemp(prefix="panoviewer-trend-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.cache, ignore_errors=True)

    def tiled(self, name, tiles=2):
        d = os.path.join(self.cache, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "config.json"), "w") as h:
            h.write("{}")
        for i in range(tiles):
            with open(os.path.join(d, "t%d.jpg" % i), "wb") as h:
                h.write(b"x" * 1000)

    def preview(self, name, size=500):
        with open(os.path.join(self.cache, name + ".jpg"), "wb") as h:
            h.write(b"x" * size)

    def test_it_counts_tiled_untitled_and_previews(self):
        self.tiled("aaa")
        self.tiled("bbb")
        os.makedirs(os.path.join(self.cache, "ccc"))          # abandoned: no config.json
        self.preview("ddd")
        self.preview("eee")
        counts = check_uptime.survey(self.cache)
        self.assertEqual(counts["tiled"], 2)
        self.assertEqual(counts["untitled"], 1)
        self.assertEqual(counts["previews"], 2)
        self.assertGreaterEqual(counts["bytes"], 2 * 2000 + 2 * 500)

    def test_the_line_is_one_line_with_the_numbers(self):
        self.tiled("aaa")
        self.preview("bbb")
        lines = []
        original = check_uptime._append_log
        check_uptime._append_log = lambda ok, message, tag=None: lines.append((tag, message))
        try:
            counts = check_uptime.trend(cache_dir=self.cache, log=lambda _m: None)
        finally:
            check_uptime._append_log = original
        self.assertEqual(counts["tiled"], 1)
        self.assertEqual(len(lines), 1)
        tag, message = lines[0]
        self.assertEqual(tag, "TREND")
        self.assertIn("tiled=1", message)
        self.assertIn("untitled=0", message)
        self.assertIn("cache=", message)

    def test_a_missing_cache_is_not_a_crash(self):
        counts = check_uptime.survey(os.path.join(self.cache, "nope"))
        self.assertEqual(counts["tiled"], 0)


class MetadataCacheTests(unittest.TestCase):
    def test_a_lookup_is_reused_within_the_window(self):
        calls = []
        now = [1000.0]
        cache = panoviewer.MetadataCache(lambda n: calls.append(n) or {"width": 1},
                                         ttl=30, clock=lambda: now[0])
        cache("x"), cache("x"), cache("x")
        self.assertEqual(calls, ["x"])

    def test_it_refreshes_after_the_window(self):
        calls = []
        now = [1000.0]
        cache = panoviewer.MetadataCache(lambda n: calls.append(n) or {"width": 1},
                                         ttl=30, clock=lambda: now[0])
        cache("x")
        now[0] += 31
        cache("x")
        self.assertEqual(len(calls), 2)

    def test_a_miss_is_not_cached(self):
        # a file uploaded a moment ago must be visible immediately
        calls = []
        cache = panoviewer.MetadataCache(lambda n: calls.append(n) or None, ttl=30)
        cache("nope"), cache("nope")
        self.assertEqual(len(calls), 2)

    def test_entries_are_independent(self):
        calls = []
        cache = panoviewer.MetadataCache(lambda n: calls.append(n) or {"width": len(n)}, ttl=30)
        cache("a"), cache("b"), cache("a")
        self.assertEqual(calls, ["a", "b"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

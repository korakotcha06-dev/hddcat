"""A search box must match what was typed, and admit what it left out.

Two ways the old search misled: SQL LIKE treats _ and % as wildcards, so a
filename pattern silently matched more than it should; and the 500-row cut
happened after ORDER BY drive_label, so a common word simply never showed
anything on the drives late in the alphabet.

Run: python3 -m unittest discover -s tests
"""
import importlib.util
import os
import shutil
import sqlite3
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_catalog():
    spec = importlib.util.spec_from_file_location(
        "catalog_under_test", os.path.join(_ROOT, "catalog.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


catalog = _load_catalog()


class SearchLiterals(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hddcat-search-")
        self.db = os.path.join(self.tmp, "t.db")
        catalog.get_conn(self.db).close()
        self.conn = sqlite3.connect(self.db)
        for relpath in ["job/shot_001.arw",      # the literal name
                        "job/shot0001.arw",      # _ as a wildcard would match this
                        "job/shotX001.arw",      # and this
                        "job/100%_final.mov",    # a literal percent
                        "job/unrelated.txt"]:
            self.conn.execute(
                "INSERT INTO files VALUES (?,?,?,?,?,?,?,?)",
                ("A", relpath, os.path.basename(relpath), ".x", 10, 0.0, "job", 0.0))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _names(self, kw):
        return sorted(os.path.basename(r[1])
                      for r in catalog.search_files(self.conn, kw))

    def test_underscore_is_a_literal_not_a_wildcard(self):
        self.assertEqual(self._names("shot_001"), ["shot_001.arw"])

    def test_percent_is_a_literal_not_a_wildcard(self):
        self.assertEqual(self._names("100%_final"), ["100%_final.mov"])

    def test_a_lone_percent_matches_nothing_rather_than_the_whole_catalog(self):
        self.assertEqual(self._names("%"), ["100%_final.mov"])

    def test_ordinary_substring_search_is_unchanged(self):
        self.assertEqual(self._names("shot"),
                         ["shot0001.arw", "shotX001.arw", "shot_001.arw"])


class SearchTotals(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hddcat-total-")
        self.db = os.path.join(self.tmp, "t.db")
        catalog.get_conn(self.db).close()
        self.conn = sqlite3.connect(self.db)
        # 30 matches spread over two drives, so a limit of 10 cuts the second
        # drive off entirely - the alphabetical-truncation trap
        for drive in ("AAA", "ZZZ"):
            for i in range(15):
                rel = "job/clip%03d.mov" % i
                self.conn.execute(
                    "INSERT INTO files VALUES (?,?,?,?,?,?,?,?)",
                    (drive, rel, "clip%03d.mov" % i, ".mov", 10, 0.0, "job", 0.0))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_count_reports_every_match_not_the_page_size(self):
        self.assertEqual(catalog.count_search_files(self.conn, "clip"), 30)
        self.assertEqual(len(catalog.search_files(self.conn, "clip", limit=10)), 10)

    def test_the_truncated_page_really_does_lose_a_whole_drive(self):
        # not a bug being fixed - the reason the honest total matters
        shown = {r[0] for r in catalog.search_files(self.conn, "clip", limit=10)}
        self.assertEqual(shown, {"AAA"})
        self.assertEqual(catalog.count_search_files(self.conn, "clip"), 30)

    def test_count_and_rows_agree_when_nothing_is_truncated(self):
        self.assertEqual(catalog.count_search_files(self.conn, "clip042"), 0)
        self.assertEqual(catalog.search_files(self.conn, "clip042"), [])


class TopsCacheThreadSafety(unittest.TestCase):
    """Stress guard, not a reproduction.

    drive_top_entries used to do `if key in cache: return cache[key]` while
    another thread could clear() between the two - a KeyError, surfacing as an
    occasional 500 on /api/drives. The window is a couple of bytecodes wide, so
    hammering it does not reliably trigger it and this test passes on the old
    code too. It is here to catch a future rewrite that reintroduces a wider
    version of the same shape; the fix itself is correct by construction.
    """

    def test_concurrent_readers_do_not_hit_a_cleared_cache(self):
        import threading

        tmp = tempfile.mkdtemp(prefix="hddcat-tops-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        db = os.path.join(tmp, "t.db")
        conn = catalog.get_conn(db)
        # more than the 64-entry ceiling, so the eviction clear() actually fires
        # while other threads are mid-lookup - without that, the race never runs
        for k in range(120):
            label = "HDD-%03d" % k
            conn.execute("INSERT OR REPLACE INTO drives VALUES (?,?,?,?)",
                         (label, 100, 50, float(k)))
            conn.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?,?)",
                         (label, "top/f.arw", "f.arw", ".arw", 10, 0.0, "top", 0.0))
        conn.commit()
        conn.close()

        catalog._TOPS_CACHE.clear()
        errors = []

        def hammer():
            c = catalog.get_conn(db)
            try:
                for _ in range(40):
                    for k in range(120):
                        catalog.drive_top_entries(c, "HDD-%03d" % k)
            except Exception as e:       # KeyError from the check-then-read race
                errors.append(e)
            finally:
                c.close()

        threads = [threading.Thread(target=hammer) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [], "the cache must survive concurrent eviction")


if __name__ == "__main__":
    unittest.main()

"""The smart library is expensive; it must be built once per catalog state.

build_smart_folders reads every row in `files`. Memoising it is only safe if
the cache still notices a new scan, and if sorting one caller's copy does not
reorder everybody else's.

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


class SmartFolderCache(unittest.TestCase):
    def setUp(self):
        catalog._RECLAIM_CACHE.clear()
        self.tmp = tempfile.mkdtemp(prefix="hddcat-cache-")
        self.db = os.path.join(self.tmp, "t.db")
        self._scan("A", [("Acme_Wedding_2024", "a.arw", 3000),
                         ("Beta_Corp_2023", "b.arw", 9000),
                         ("Zulu_Shoot_2025", "c.arw", 1000)])

    def tearDown(self):
        catalog._RECLAIM_CACHE.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _scan(self, label, folders):
        d = os.path.join(self.tmp, label)
        for folder, fname, size in folders:
            os.makedirs(os.path.join(d, folder), exist_ok=True)
            with open(os.path.join(d, folder, fname), "wb") as f:
                f.write(b"\0" * size)
        catalog.scan_drive(self.db, d, label)

    def _count_builds(self):
        """Wrap build_smart_folders so we can see whether it actually ran."""
        real = catalog.build_smart_folders
        calls = []

        def counting(rows):
            calls.append(1)
            return real(rows)

        catalog.build_smart_folders = counting
        self.addCleanup(setattr, catalog, "build_smart_folders", real)
        return calls

    def test_same_rows_as_building_it_directly(self):
        conn = sqlite3.connect(self.db)
        rows = conn.execute(
            "SELECT drive_label, relpath, size, mtime FROM files").fetchall()
        expected = catalog.sort_folders(catalog.build_smart_folders(rows), "client")
        got = catalog.sort_folders(list(catalog.smart_folders_cached(conn)), "client")
        conn.close()
        self.assertEqual([(g["drive"], g["folder"], g["size"]) for g in got],
                         [(e["drive"], e["folder"], e["size"]) for e in expected])

    def test_second_call_does_not_rebuild(self):
        conn = sqlite3.connect(self.db)
        calls = self._count_builds()
        catalog.smart_folders_cached(conn)
        catalog.smart_folders_cached(conn)
        catalog.smart_folders_cached(conn)
        conn.close()
        self.assertEqual(len(calls), 1, "three requests, one build")

    def test_a_new_scan_invalidates_it(self):
        conn = sqlite3.connect(self.db)
        calls = self._count_builds()
        before = catalog.smart_folders_cached(conn)
        conn.close()

        self._scan("B", [("Gamma_Shoot_2025", "c.arw", 5000)])

        conn = sqlite3.connect(self.db)
        after = catalog.smart_folders_cached(conn)
        conn.close()
        self.assertEqual(len(calls), 2, "the new drive forces a rebuild")
        self.assertGreater(len(after), len(before), "and the new folder shows up")

    def test_sorting_a_copy_does_not_reorder_the_cache(self):
        conn = sqlite3.connect(self.db)
        before = [f["folder"] for f in catalog.smart_folders_cached(conn)]
        by_size = [f["folder"] for f in
                   catalog.sort_folders(list(catalog.smart_folders_cached(conn)), "size")]
        after = [f["folder"] for f in catalog.smart_folders_cached(conn)]
        conn.close()
        if before == by_size:  # nothing to catch if both orders coincide
            self.skipTest("build order already matches size order")
        self.assertEqual(before, after,
                         "sort_folders sorts in place - it must only ever see a copy")


if __name__ == "__main__":
    unittest.main()

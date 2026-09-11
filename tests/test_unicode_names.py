"""The same filename on two drives must be the same string.

macOS hands back different byte sequences for one name depending on the
filesystem: HFS+ decomposes (e + combining acute), APFS keeps what was typed.
Copy a folder from an old drive to a new one and the catalog ends up holding
two spellings of one name - which the duplicate finder cannot pair up and the
search box cannot find.

Run: python3 -m unittest discover -s tests
"""
import importlib.util
import os
import shutil
import sqlite3
import tempfile
import unicodedata
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

NAME = "Café_Wedding.arw"
NFC = unicodedata.normalize("NFC", NAME)
NFD = unicodedata.normalize("NFD", NAME)


def _load_catalog():
    spec = importlib.util.spec_from_file_location(
        "catalog_under_test", os.path.join(_ROOT, "catalog.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


catalog = _load_catalog()


class UnicodeNames(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(NFC, NFD, "test premise: the two forms differ")
        self.tmp = tempfile.mkdtemp(prefix="hddcat-uni-")
        self.db = os.path.join(self.tmp, "t.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_drive(self, label, filename, size=2_000_000):
        d = os.path.join(self.tmp, label)
        os.makedirs(os.path.join(d, "2024_Job"), exist_ok=True)
        with open(os.path.join(d, "2024_Job", filename), "wb") as f:
            f.write(b"\0" * size)
        return d

    def _stored_names(self):
        conn = sqlite3.connect(self.db)
        rows = [r[0] for r in conn.execute("SELECT filename FROM files")]
        conn.close()
        return rows

    def test_decomposed_name_is_stored_composed(self):
        drive = self._make_drive("OLD", NFD, size=10)
        on_disk = os.listdir(os.path.join(drive, "2024_Job"))[0]
        if on_disk != NFD:  # filesystem composed it for us; nothing to prove
            self.skipTest("this filesystem does not preserve NFD names")
        catalog.scan_drive(self.db, drive, "OLD")
        self.assertEqual(self._stored_names(), [NFC])

    def test_same_file_on_hfs_and_apfs_drives_is_one_duplicate_group(self):
        old = self._make_drive("OLD", NFD)
        new = self._make_drive("NEW", NFC)
        if os.listdir(os.path.join(old, "2024_Job"))[0] != NFD:
            self.skipTest("this filesystem does not preserve NFD names")
        catalog.scan_drive(self.db, old, "OLD")
        catalog.scan_drive(self.db, new, "NEW")
        conn = sqlite3.connect(self.db)
        groups, _waste, group_count = catalog.build_dedup(conn, min_size=1000)
        conn.close()
        self.assertEqual(group_count, 1,
                         "one file on two drives is one duplicate group, not zero")
        self.assertEqual(groups[0]["copies"], 2)

    def test_search_finds_names_a_previous_version_stored_decomposed(self):
        # a catalog built before NFC normalisation: the row is NFD on disk
        conn = sqlite3.connect(self.db)
        catalog.get_conn(self.db).close()  # create the schema
        conn.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?,?)",
                     ("OLD", "2024_Job/" + NFD, NFD, ".arw", 10, 0.0, "2024_Job", 0.0))
        conn.commit()
        hits = catalog.search_files(conn, "Café")
        conn.close()
        self.assertEqual(len(hits), 1,
                         "searching the composed form must still find an old NFD row")

    def test_search_still_matches_plain_ascii(self):
        drive = self._make_drive("A", "shot001.arw", size=10)
        catalog.scan_drive(self.db, drive, "A")
        conn = sqlite3.connect(self.db)
        self.assertEqual(len(catalog.search_files(conn, "shot")), 1)
        self.assertEqual(len(catalog.search_files(conn, "nope")), 0)
        conn.close()


if __name__ == "__main__":
    unittest.main()

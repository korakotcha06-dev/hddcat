#!/usr/bin/env python3
"""
HDD Catalog & Consolidation Tool
=================================
Catalogs files across multiple external HDDs into one searchable SQLite database
(no need to have every drive plugged in at once to know what's on it), and
suggests consolidation opportunities based on folder-name similarity across drives.

USAGE
  python3 catalog.py scan <drive_path> --label <DRIVE_LABEL>
  python3 catalog.py search <keyword>
  python3 catalog.py report
  python3 catalog.py groups [--threshold 0.72] [--min-drives 2]
  python3 catalog.py dedup [--min-size-mb 1]
  python3 catalog.py reclaim [--junk] [--big-old] [--by-type]
  python3 catalog.py serve [--port 8765] [--no-browser]
  python3 catalog.py export-obsidian <vault_folder>

  Global option --db <path>  (default: catalog.db in current directory)
  Put --db BEFORE the subcommand, e.g.:
    python3 catalog.py --db /path/to/catalog.db scan /Volumes/WD-4TB-01 --label WD-4TB-01

WORKFLOW
  1. Plug in a drive. Run `scan` on it with a label you'll reuse every time
     (write the label on a sticker on the physical drive).
  2. Repeat for every drive you own, whenever convenient (doesn't need to be
     the same session).
  3. `search <keyword>` works even with the drive unplugged - it tells you
     WHICH drive has the file, so you know what to go plug in.
  4. `groups` looks for folders with similar names living on different drives
     and suggests which one to consolidate onto.
  5. `export-obsidian` writes one markdown note per drive into your vault so
     you can browse/search the catalog from Obsidian itself.
"""
__version__ = "1.2.0"

import argparse
import json
import os
import re
import sqlite3
import shutil
import subprocess
import sys
import threading
import time
import difflib
import webbrowser
import zipfile
import tempfile
import urllib.request
from collections import defaultdict

DB_DEFAULT = "catalog.db"
SKIP_DIRS = {".Trash", "$RECYCLE.BIN", ".Spotlight-V100", ".fseventsd", ".TemporaryItems",
             "System Volume Information"}
SKIP_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}


def human_size(n):
    n = float(n or 0)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(n) < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def get_conn(db_path):
    conn = sqlite3.connect(db_path)
    # web UI queries and a background scan may hit the DB at the same moment -
    # wait instead of throwing "database is locked" (no-op for plain CLI use)
    conn.execute("PRAGMA busy_timeout=10000")
    # WAL: readers keep working while a long scan transaction writes
    # (rollback-journal mode locked the whole UI during big scans);
    # NORMAL sync is the standard WAL pairing - safe for a rebuildable catalog
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS files (
        drive_label TEXT, relpath TEXT, filename TEXT, ext TEXT,
        size INTEGER, mtime REAL, depth1 TEXT, scanned_at REAL,
        PRIMARY KEY (drive_label, relpath)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS drives (
        drive_label TEXT PRIMARY KEY, total_bytes INTEGER, free_bytes INTEGER, last_scanned REAL
    )""")
    return conn


def scan_drive(db_path, drive_path, label, progress=None):
    """Core scan logic (shared by CLI and web UI). Opens its own connection so it
    can run in a background thread. Calls progress(count) every 5000 files.
    Returns a stats dict; raises ValueError if drive_path is not a directory."""
    drive_path = os.path.abspath(drive_path)
    if not os.path.isdir(drive_path):
        raise ValueError(f"ไม่พบ path {drive_path}")
    conn = get_conn(db_path)
    t0 = time.time()
    conn.execute("DELETE FROM files WHERE drive_label=?", (label,))
    count = 0
    total_bytes = 0
    for root, dirs, files in os.walk(drive_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for fname in files:
            if fname in SKIP_FILES:
                continue
            full = os.path.join(root, fname)
            try:
                st = os.stat(full)
            except OSError:
                continue
            relpath = os.path.relpath(full, drive_path)
            parts = relpath.split(os.sep)
            depth1 = parts[0] if len(parts) > 1 else ""
            ext = os.path.splitext(fname)[1].lower()
            conn.execute(
                "INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?)",
                (label, relpath, fname, ext, st.st_size, st.st_mtime, depth1, time.time())
            )
            count += 1
            total_bytes += st.st_size
            if count % 5000 == 0 and progress:
                progress(count)
    try:
        usage = shutil.disk_usage(drive_path)
        disk_total, disk_free = usage.total, usage.free
    except OSError:
        disk_total, disk_free = None, None
    conn.execute("INSERT OR REPLACE INTO drives VALUES (?,?,?,?)",
                 (label, disk_total, disk_free, time.time()))
    conn.commit()
    conn.close()
    return {"files": count, "bytes": total_bytes, "seconds": time.time() - t0,
            "disk_total": disk_total, "disk_free": disk_free}


def cmd_scan(args):
    drive_path = os.path.abspath(args.drive_path)
    if not os.path.isdir(drive_path):
        print(f"ERROR: ไม่พบ path {drive_path}")
        sys.exit(1)
    label = args.label
    print(f"Scanning {drive_path} as '{label}' ...")
    res = scan_drive(args.db, drive_path, label,
                     progress=lambda c: print(f"  ...{c} files so far"))
    print(f"Done: {res['files']} files, {human_size(res['bytes'])} cataloged in {res['seconds']:.1f}s")
    if res["disk_total"]:
        print(f"Drive capacity: {human_size(res['disk_total'])} total, {human_size(res['disk_free'])} free "
              f"({res['disk_free']/res['disk_total']*100:.0f}% free)")


def search_files(conn, keyword, limit=None):
    """Shared search logic (CLI + web). Returns rows of (drive_label, relpath, size, mtime)."""
    kw = f"%{keyword}%"
    sql = ("SELECT drive_label, relpath, size, mtime FROM files "
           "WHERE filename LIKE ? OR relpath LIKE ? ORDER BY drive_label, relpath")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, (kw, kw)).fetchall()


def cmd_search(args):
    conn = get_conn(args.db)
    rows = search_files(conn, args.keyword)
    if not rows:
        print("ไม่เจอไฟล์ที่ตรงกับคำค้นหา")
        return
    print(f"เจอ {len(rows)} ไฟล์:\n")
    for drive_label, relpath, size, mtime in rows:
        mdate = time.strftime("%Y-%m-%d", time.localtime(mtime))
        print(f"[{drive_label}] {relpath}  ({human_size(size)}, {mdate})")


def drives_overview(conn):
    """Shared per-drive summary (CLI report + web drive cards). Returns list of dicts."""
    drives = conn.execute(
        "SELECT drive_label, total_bytes, free_bytes, last_scanned FROM drives ORDER BY drive_label"
    ).fetchall()
    out = []
    for label, total_bytes, free_bytes, last_scanned in drives:
        n, s = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size),0) FROM files WHERE drive_label=?", (label,)
        ).fetchone()
        out.append({"label": label, "total_bytes": total_bytes, "free_bytes": free_bytes,
                    "last_scanned": last_scanned, "files": n, "bytes": s})
    return out


def cmd_report(args):
    conn = get_conn(args.db)
    overview = drives_overview(conn)
    if not overview:
        print("ยังไม่มี drive ไหนถูก scan")
        return
    for d in overview:
        label, total_bytes, free_bytes, last_scanned = (
            d["label"], d["total_bytes"], d["free_bytes"], d["last_scanned"])
        n, s = d["files"], d["bytes"]
        scanned_date = time.strftime("%Y-%m-%d %H:%M", time.localtime(last_scanned))
        print(f"\n=== {label} ===")
        print(f"scanned: {scanned_date}")
        print(f"cataloged: {n} files, {human_size(s)}")
        if total_bytes:
            print(f"disk: {human_size(total_bytes)} total, {human_size(free_bytes)} free "
                  f"({free_bytes/total_bytes*100:.0f}%)")
        top_ext = conn.execute(
            "SELECT ext, COUNT(*) c, SUM(size) s FROM files WHERE drive_label=? "
            "GROUP BY ext ORDER BY s DESC LIMIT 5",
            (label,)
        ).fetchall()
        for ext, c, s in top_ext:
            print(f"  {ext or '(no ext)'}: {c} files, {human_size(s)}")


def normalize_name(name):
    name = name.lower()
    name = re.sub(r'[_\-\s]+', ' ', name)
    name = re.sub(r'[^\w\s]', '', name)
    name = re.sub(r'\b(20\d{2}|19\d{2})\b', '', name)  # strip year numbers
    name = re.sub(r'\s+', ' ', name).strip()
    return name


_MONTH = r'(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)'
_DATE_PATTERNS = [
    re.compile(r'\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2}', re.I),  # 2025.09.11 / 2026-03-10
    re.compile(r'\d{4}[.\-/]\d{1,2}', re.I),                # 2026-04
    re.compile(r'\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}', re.I), # 25.10.12
    re.compile(r'\d{1,2}[.\-/]\d{1,2}', re.I),              # 12-07
    re.compile(r'\d{1,2}\s*' + _MONTH + r'\w*', re.I),      # 25Feb, 25 Feb
    re.compile(_MONTH + r'\w*\s*\d{1,2}', re.I),            # Feb25
    re.compile(r'\b(19|20)\d{2}\b'),                        # bare 4-digit year
]
_STOPWORDS = {"at", "for", "the", "of", "a", "an", "and", "in", "on", "to", "with"}


def client_tokens(name):
    """Strip date/month fragments, then return significant word tokens (client-name candidates)."""
    for pat in _DATE_PATTERNS:
        name = pat.sub(' ', name)
    name = name.lower()
    raw = re.findall(r'[^\W_]+', name, re.UNICODE)
    return set(t for t in raw if len(t) >= 3 and t not in _STOPWORDS and not t.isdigit())


def cmd_groups(args):
    conn = get_conn(args.db)
    rows = conn.execute(
        "SELECT drive_label, depth1, COUNT(*), SUM(size) FROM files "
        "WHERE depth1 != '' GROUP BY drive_label, depth1"
    ).fetchall()
    if not rows:
        print("ไม่มีข้อมูลพอจะจัดกลุ่ม (scan อย่างน้อย 2 drive ก่อน)")
        return

    if args.by_client:
        cmd_groups_by_client(args, rows)
        return

    items = [{"drive": d, "name": name, "norm": normalize_name(name), "count": c, "size": s}
             for d, name, c, s in rows]
    n = len(items)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        for j in range(i + 1, n):
            if items[i]["drive"] == items[j]["drive"]:
                continue  # only cluster across DIFFERENT drives
            a, b = items[i]["norm"], items[j]["norm"]
            if not a or not b:
                continue
            ratio = difflib.SequenceMatcher(None, a, b).ratio()
            ac, bc = a.replace(" ", ""), b.replace(" ", "")  # catch "NKMedic" vs "NK Medic Group"
            if ratio >= args.threshold or a in b or b in a or ac in bc or bc in ac:
                union(i, j)

    clusters = defaultdict(list)
    for i in range(n):
        clusters[find(i)].append(items[i])

    results = []
    for members in clusters.values():
        drives_involved = set(m["drive"] for m in members)
        if len(drives_involved) < args.min_drives:
            continue
        total_size = sum(m["size"] for m in members)
        results.append((total_size, members))
    results.sort(key=lambda x: -x[0])

    if not results:
        print(f"ไม่เจอกลุ่มที่ folder ชื่อคล้ายกันอยู่คนละ drive ({args.min_drives}+ drives) "
              f"ที่ threshold {args.threshold}")
        print("ลองลด --threshold ลง (เช่น 0.6) ถ้าคิดว่าควรเจอมากกว่านี้")
        return

    print(f"เจอ {len(results)} กลุ่มที่กระจายอยู่คนละ drive:\n")
    for total_size, members in results:
        names = sorted(set(m["name"] for m in members))
        label = " / ".join(names[:3]) + (" ..." if len(names) > 3 else "")
        print(f"--- {label}  (รวม {human_size(total_size)}) ---")
        members_sorted = sorted(members, key=lambda m: -m["size"])
        for m in members_sorted:
            print(f"  [{m['drive']}] {m['name']}  -  {m['count']} files, {human_size(m['size'])}")
        target = members_sorted[0]["drive"]
        others = sorted(set(m["drive"] for m in members_sorted) - {target})
        print(f"  แนะนำ: รวมเข้า [{target}] (มีของกลุ่มนี้เยอะสุดอยู่แล้ว) ย้ายจาก {', '.join(others)}")
        print()


def cmd_groups_by_client(args, rows):
    items = [{"drive": d, "name": name, "tokens": client_tokens(name), "count": c, "size": s}
             for d, name, c, s in rows]
    n = len(items)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    token_index = defaultdict(list)
    for i, it in enumerate(items):
        for tok in it["tokens"]:
            token_index[tok].append(i)

    # ignore tokens that show up across too many different folders - those are
    # generic words (e.g. "final", "video", "event"), not client names
    for tok, idxs in token_index.items():
        if len(set(idxs)) > args.max_token_spread:
            continue
        for k in range(1, len(idxs)):
            union(idxs[0], idxs[k])

    clusters = defaultdict(list)
    for i in range(n):
        clusters[find(i)].append(items[i])

    results = []
    for members in clusters.values():
        if len(members) < args.min_jobs:
            continue
        total_size = sum(m["size"] for m in members)
        results.append((total_size, members))
    results.sort(key=lambda x: -x[0])

    if not results:
        print(f"ไม่เจอกลุ่มลูกค้าที่มีงานซ้ำ {args.min_jobs}+ งานขึ้นไป "
              f"(ตัด date/เดือนออกแล้ว เทียบ token ที่เหลือ)")
        print("ลอง --min-jobs 2 หรือเช็คว่าชื่อ folder มี pattern วันที่ที่ script ยังไม่รู้จักไหม")
        return

    print(f"เจอ {len(results)} กลุ่มลูกค้า/บริษัท (รวมทุก drive, ตัด ปี/เดือน/วันที่ ออกจากชื่อแล้ว):\n")
    for total_size, members in results:
        names = sorted(set(m["name"] for m in members))
        drives_involved = sorted(set(m["drive"] for m in members))
        print(f"--- {' | '.join(names)}  (รวม {human_size(total_size)}, "
              f"{len(members)} งาน, drive: {', '.join(drives_involved)}) ---")
        for m in sorted(members, key=lambda m: -m["size"]):
            print(f"  [{m['drive']}] {m['name']}  -  {m['count']} files, {human_size(m['size'])}")
        print()


_YEAR_ONLY = re.compile(r'^(19|20)\d{2}$')
# leading date at start of a folder name: 22-07-07, 2025.09.11, 2026-04, 12-07
_LEADING_DATE = re.compile(r'^(\d{2,4})[-._](\d{1,2})(?:[-._](\d{1,2}))?')


def leading_date(name):
    """Return the raw leading date string of a folder name, or None. Does NOT convert
    CE/BE - the 2-digit prefix is kept as-is because it's ambiguous."""
    m = _LEADING_DATE.match(name.strip())
    return m.group(0) if m else None


def normalize_date(name, ref_year=None):
    """Parse the leading date and return a normalized CE 'YYYY-MM-DD' (or 'YYYY-MM'),
    plus a sort tuple. Rules:
      - 4-digit 2400-2600 -> BE, convert to CE (-543)
      - 4-digit other     -> CE as-is
      - 2-digit YY (ambiguous) -> compute both CE(20YY) and BE-short(1957+YY),
        pick whichever CE year is closest to ref_year (the folder's file mtime year).
        Falls back to CE 20YY when ref_year is unknown.
    Returns (display_str, sort_tuple, cal) where cal is 'CE'/'BE'/'' for transparency.
    """
    m = _LEADING_DATE.match(name.strip())
    if not m:
        return "", None, ""
    y, mo, da = m.group(1), m.group(2), m.group(3)
    raw = int(y)
    cal = ""
    if len(y) <= 2:
        ce_year = 2000 + raw          # e.g. 22 -> 2022
        be_year = 1957 + raw          # 25YY -> CE : (2500+YY)-543 = 1957+YY, e.g. 65 -> 2022
        this_year = time.localtime().tm_year
        if ce_year > this_year + 1:
            # CE reading would be an implausible future (e.g. 67 -> 2067) -> must be BE short
            yi, cal = be_year, "BE"
        elif ref_year:
            yi = ce_year if abs(ce_year - ref_year) <= abs(be_year - ref_year) else be_year
            cal = "CE" if yi == ce_year else "BE"
        else:
            yi, cal = ce_year, "CE"
    elif 2400 <= raw <= 2600:
        yi, cal = raw - 543, "BE"
    else:
        yi, cal = raw, "CE"
    mi = int(mo)
    di = int(da) if da else 0
    disp = f"{yi:04d}-{mi:02d}-{di:02d}" if di else f"{yi:04d}-{mi:02d}"
    return disp, (yi, mi, di), cal


NO_CLIENT = "(no client)"


def build_smart_folders(rows):
    """Smart-depth aggregation (shared by export-folders-csv and the web Library view).
    Takes raw (drive_label, relpath, size, mtime) rows, returns unsorted list of dicts
    with drive/type/client/job/date/dnorm/dsort/cal/myear/count/size keys."""
    # Pass 1: for each (drive, top-folder) find whether it wraps date-prefixed jobs
    top_children = defaultdict(set)   # (drive, d1) -> set of immediate child folder names
    for drive_label, relpath, size, mtime in rows:
        parts = relpath.split(os.sep)
        if len(parts) > 2:
            top_children[(drive_label, parts[0])].add(parts[1])
    is_wrapper = {}
    for key, kids in top_children.items():
        drive_label, d1 = key
        if leading_date(d1):
            is_wrapper[key] = False       # d1 is itself a dated job
        else:
            is_wrapper[key] = any(leading_date(k) for k in kids)

    # Pass 2: assign each file to a (folder, type) bucket
    # bucket -> [count, size, min_mt, max_mt]
    agg = defaultdict(lambda: [0, 0, None, None])
    for drive_label, relpath, size, mtime in rows:
        parts = relpath.split(os.sep)
        if len(parts) == 1:
            folder, typ = "(root)", "other"
        else:
            d1 = parts[0]
            key1 = (drive_label, d1)
            if leading_date(d1):
                folder, typ = d1, "job"
            elif is_wrapper.get(key1):
                if len(parts) > 2:
                    child = parts[1]
                    folder = d1 + os.sep + child
                    typ = "job" if leading_date(child) else "asset"
                else:
                    folder = d1 + os.sep + "(loose files)"
                    typ = "asset"
            else:
                folder, typ = d1, "other"
        rec = agg[(drive_label, folder, typ)]
        rec[0] += 1
        rec[1] += size
        rec[2] = mtime if rec[2] is None else min(rec[2], mtime)
        rec[3] = mtime if rec[3] is None else max(rec[3], mtime)

    # build output rows with client/job split
    out = []
    for (drive_label, folder, typ), (cnt, size, min_mt, max_mt) in agg.items():
        fparts = folder.split(os.sep)
        if len(fparts) >= 2:
            client = fparts[0]
            job = os.sep.join(fparts[1:])
        else:
            client = NO_CLIENT
            job = folder
        dname = leading_date(job) or ""
        ref_year = int(time.strftime("%Y", time.localtime(min_mt))) if min_mt else None
        dnorm, dsort, cal = normalize_date(job, ref_year)
        yo = time.strftime("%Y", time.localtime(min_mt)) if min_mt else ""
        yn = time.strftime("%Y", time.localtime(max_mt)) if max_mt else ""
        myear = yo if yo == yn else f"{yo}-{yn}"
        out.append({
            "drive": drive_label, "type": typ, "client": client, "job": job,
            "date": dname, "dnorm": dnorm, "dsort": dsort, "cal": cal,
            "myear": myear, "count": cnt, "size": size,
            "folder": folder,
        })
    return out


def sort_folders(out, sort="client"):
    """Shared sort for smart-depth rows: 'client' = client then date (default), 'size' = largest first."""
    def date_key(d):
        # normalized CE date sorts chronologically within a client;
        # no-date rows (assets) sort last within their client block
        return (0, d["dsort"]) if d["dsort"] else (1, (0, 0, 0))

    if sort == "size":
        out.sort(key=lambda d: -d["size"])
    else:  # client -> date (default)
        out.sort(key=lambda d: (d["client"] == NO_CLIENT, d["client"].lower(),
                                date_key(d), -d["size"]))
    return out


def cmd_export_folders_csv(args):
    import csv
    conn = get_conn(args.db)
    rows = conn.execute("SELECT drive_label, relpath, size, mtime FROM files").fetchall()
    if not rows:
        print("ยังไม่มีข้อมูล (scan ก่อน)")
        return
    depth = args.depth
    smart = args.smart_depth

    if not smart:
        # ---- fixed-depth mode (unchanged) ----
        agg = defaultdict(lambda: [0, 0, None, None])
        for drive_label, relpath, size, mtime in rows:
            parts = relpath.split(os.sep)
            if len(parts) > depth:
                folder = os.sep.join(parts[:depth])
            elif len(parts) > 1:
                folder = os.sep.join(parts[:-1])
            else:
                folder = "(root)"
            rec = agg[(drive_label, folder)]
            rec[0] += 1
            rec[1] += size
            rec[2] = mtime if rec[2] is None else min(rec[2], mtime)
            rec[3] = mtime if rec[3] is None else max(rec[3], mtime)
        items = sorted(agg.items(), key=lambda kv: -kv[1][1])
        with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["drive_label", "folder", "mtime_year", "file_count", "size_human"])
            for (drive_label, folder), (cnt, size, min_mt, max_mt) in items:
                yo = time.strftime("%Y", time.localtime(min_mt)) if min_mt else ""
                yn = time.strftime("%Y", time.localtime(max_mt)) if max_mt else ""
                year = yo if yo == yn else f"{yo}-{yn}"
                w.writerow([drive_label, folder, year, cnt, human_size(size)])
        print(f"wrote {len(items)} folder groups (depth={depth}) to {args.output}, sorted by size desc")
        print("คอลัมน์ mtime_year = ปีจากวันที่แก้ไขไฟล์ (อาจเพี้ยนถ้าเคย copy ข้าม drive)")
        return

    # ---- smart-depth mode ----  (logic lives in build_smart_folders/sort_folders,
    # shared with the web UI Library view)
    out = sort_folders(build_smart_folders(rows), args.sort)

    njob = sum(1 for d in out if d["type"] == "job")
    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["drive_label", "type", "client", "job", "date_norm", "cal",
                    "date_in_name", "mtime_year", "file_count", "size_human"])
        for d in out:
            w.writerow([d["drive"], d["type"], d["client"], d["job"], d["dnorm"], d["cal"],
                        d["date"], d["myear"], d["count"], human_size(d["size"])])
    sort_desc = "client แล้ววันที่" if args.sort == "client" else "ขนาดใหญ่->เล็ก"
    print(f"wrote {len(out)} rows (smart-depth) to {args.output}, เรียงตาม {sort_desc}")
    print(f"  จับเป็นงาน (type=job) {njob} งาน - folder ที่ชื่อขึ้นต้นด้วยวันที่")
    print("  type=asset = โฟลเดอร์ใช้ร่วม (Music Library ฯลฯ), type=other = อื่นๆ")
    print("  client/job แยกคอลัมน์แล้ว - ใน Excel group ตาม client ได้เลย, filter type=job ดูเฉพาะงาน")
    print("  date_norm = ค.ศ. YYYY-MM-DD; cal = ระบบเดาว่าชื่อเดิมเป็น ค.ศ.(CE)/พ.ศ.(BE) โดยดู mtime ช่วย")
    print("  ถ้า cal เดาผิด ให้ดู date_in_name (ดิบ) เทียบ - เลข 2 หลักที่ mtime เพี้ยนมากอาจตัดสินผิด")


def cmd_export_csv(args):
    import csv
    conn = get_conn(args.db)
    rows = conn.execute(
        "SELECT drive_label, relpath, filename, ext, size, mtime, depth1 "
        "FROM files ORDER BY drive_label, relpath"
    ).fetchall()
    if not rows:
        print("ยังไม่มีข้อมูล (scan ก่อน)")
        return
    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["drive_label", "path", "filename", "ext",
                    "size_human", "modified", "top_folder"])
        for drive_label, relpath, filename, ext, size, mtime, depth1 in rows:
            mdate = time.strftime("%Y-%m-%d", time.localtime(mtime))
            w.writerow([drive_label, relpath, filename, ext,
                        human_size(size), mdate, depth1])
    print(f"wrote {len(rows)} rows to {args.output}")
    print("เปิดด้วย Excel/Numbers แล้วกด Cmd+A > Insert Table (หรือ Format as Table) เพื่อเปิด sort/filter")


def forget_drive(conn, label):
    """Remove one drive's rows from the catalog (files + drives). Touches ONLY the
    catalog database - never the real files on any disk."""
    n = conn.execute("SELECT COUNT(*) FROM files WHERE drive_label=?", (label,)).fetchone()[0]
    known = conn.execute("SELECT COUNT(*) FROM drives WHERE drive_label=?", (label,)).fetchone()[0]
    if n == 0 and known == 0:
        return None
    conn.execute("DELETE FROM files WHERE drive_label=?", (label,))
    conn.execute("DELETE FROM drives WHERE drive_label=?", (label,))
    conn.commit()
    return n


def build_dedup(conn, min_size=1_048_576, max_groups=500):
    """Duplicate-file candidates from catalog data only: same filename + same exact size.
    Read-only with respect to the actual files - drives don't even need to be plugged in
    (we can't hash contents for the same reason). Returns (groups, total_waste, group_count);
    groups capped at max_groups, sorted by reclaimable space desc."""
    # one-time index so the self-join is fast on ~1M rows (adds no data, changes no logic)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_name_size ON files(filename, size)")
    conn.commit()
    rows = conn.execute(
        """SELECT f.filename, f.size, f.drive_label, f.relpath, f.mtime
           FROM files f JOIN (
               SELECT filename, size FROM files
               WHERE size >= ? GROUP BY filename, size HAVING COUNT(*) > 1
           ) d ON f.filename = d.filename AND f.size = d.size
           ORDER BY f.size DESC, f.filename, f.drive_label, f.relpath""",
        (min_size,)).fetchall()
    by_key = {}
    for filename, size, drive, relpath, mtime in rows:
        by_key.setdefault((filename, size), []).append(
            {"drive": drive, "relpath": relpath,
             "mdate": time.strftime("%Y-%m-%d", time.localtime(mtime))})
    groups = []
    total_waste = 0
    for (filename, size), members in by_key.items():
        waste = size * (len(members) - 1)
        total_waste += waste
        groups.append({"filename": filename, "size": size, "size_human": human_size(size),
                       "copies": len(members), "waste": waste,
                       "waste_human": human_size(waste), "members": members})
    groups.sort(key=lambda g: -g["waste"])
    return groups[:max_groups], total_waste, len(groups)


def cmd_dedup(args):
    conn = get_conn(args.db)
    min_size = int(args.min_size_mb * 1024 * 1024)
    groups, total_waste, ngroups = build_dedup(conn, min_size=min_size, max_groups=args.limit)
    if not groups:
        print(f"ไม่เจอไฟล์ซ้ำ (เทียบชื่อไฟล์+ขนาดตรงกัน, ขนาด >= {args.min_size_mb}MB)")
        return
    print(f"เจอ {ngroups} กลุ่มไฟล์ซ้ำ (เทียบจากชื่อไฟล์+ขนาดตรงกัน - ไม่ได้อ่านเนื้อไฟล์ "
          f"เพราะ drive อาจไม่ได้เสียบอยู่)")
    print(f"พื้นที่ที่คืนได้ถ้าเก็บไว้ชุดเดียว: ~{human_size(total_waste)}\n")
    for g in groups:
        print(f"--- {g['filename']}  ({g['size_human']} x {g['copies']} ชุด, "
              f"คืนได้ {g['waste_human']}) ---")
        for m in g["members"]:
            print(f"  [{m['drive']}] {m['relpath']}")
        print()
    if ngroups > len(groups):
        print(f"(แสดง {len(groups)} กลุ่มแรกจาก {ngroups} กลุ่ม - เรียงตามพื้นที่ที่คืนได้)")


# =============================== reclaim space ===============================
# "อะไรลบ/ย้ายได้บ้าง" - answered from catalog metadata only: no drive needs to be
# plugged in, no file is ever read, and nothing here deletes anything. Every code
# path below is read-only by construction; the UI hands the user a list.

# Anything outside this window is a broken timestamp, not an old file. Real
# catalogs contain files dated 2262 and 1904 (copies, bad cameras, filesystem
# quirks) - scoring by age without this guard puts garbage at the top.
MTIME_FLOOR = 946684800.0          # 2000-01-01
YEAR_SECONDS = 365 * 86400

# Junk rules. `where` is a fixed SQL fragment (never user input). tier "safe" =
# the app that made it can regenerate it; tier "review" = looks like junk but
# losing it costs something (Auto-Save is a project backup, a proxy may still be
# linked in an open timeline).
# NOTE: .DS_Store / Thumbs.db / desktop.ini can never show up here - SKIP_FILES
# drops them at scan time, so the catalog has no record of them at all.
RECLAIM_RULES = [
    {"id": "premiere_previews", "tier": "safe",
     "label": "Premiere Video/Audio Previews",
     "note": "ไฟล์พรีวิวที่ Premiere สร้างใหม่ได้เอง",
     "where": "relpath LIKE '%Adobe Premiere Pro Video Previews%' "
              "OR relpath LIKE '%Adobe Premiere Pro Audio Previews%'"},
    {"id": "premiere_cache", "tier": "safe",
     "label": "Premiere peak/conform (.pek/.cfa)",
     "note": "ไฟล์ waveform/conform สร้างใหม่อัตโนมัติเมื่อเปิดงาน",
     "where": "ext IN ('.pek', '.cfa')"},
    {"id": "lightroom_previews", "tier": "safe",
     "label": "Lightroom previews",
     "note": "พรีวิวของ Lightroom สร้างใหม่ได้จากไฟล์ต้นฉบับ",
     "where": "ext = '.lrprev' OR relpath LIKE '%Previews.lrdata%'"},
    {"id": "resolve_cache", "tier": "safe",
     "label": "DaVinci Resolve cache / ProxyMedia",
     "note": "cache ที่ Resolve สร้างใหม่ได้",
     "where": "relpath LIKE '%CacheClip%' OR relpath LIKE '%ProxyMedia%'"},
    {"id": "fcp_render", "tier": "safe",
     "label": "Final Cut render / transcoded media",
     "note": "ไฟล์ render ของ Final Cut สร้างใหม่ได้",
     "where": "relpath LIKE '%Render Files%' OR relpath LIKE '%Transcoded Media%'"},
    {"id": "appledouble", "tier": "safe",
     "label": "AppleDouble (ไฟล์ ._)",
     "note": "เศษ metadata ที่ macOS ทิ้งไว้บนไดรฟ์ exFAT/FAT",
     "where": "filename LIKE '._%'"},
    {"id": "temp", "tier": "safe",
     "label": "ไฟล์ชั่วคราว (.tmp/.temp/.bak)",
     "note": "ของค้างจากโปรแกรมที่ปิดไม่สมบูรณ์",
     "where": "ext IN ('.tmp', '.temp', '.bak')"},
    {"id": "premiere_autosave", "tier": "review",
     "label": "Premiere Auto-Save",
     "note": "เป็นไฟล์โปรเจกต์สำรอง - ลบแล้วกู้งานย้อนหลังไม่ได้ ดูก่อนลบ",
     "where": "relpath LIKE '%Auto-Save%'"},
    {"id": "proxy", "tier": "review",
     "label": "โฟลเดอร์ proxy",
     "note": "ถ้ายังตัดงานนั้นอยู่ proxy อาจถูกลิงก์ในไทม์ไลน์ ดูก่อนลบ",
     "where": "relpath LIKE '%/Proxy/%' OR relpath LIKE '%/Proxies/%' "
              "OR relpath LIKE 'Proxy/%' OR relpath LIKE 'Proxies/%'"},
]
_RULES_BY_ID = {r["id"]: r for r in RECLAIM_RULES}

# ext -> bucket for the "พื้นที่หายไปไหน" rollup. Anything unlisted lands in "อื่นๆ".
RECLAIM_TYPES = [
    ("วิดีโอ", ['.mp4', '.mov', '.r3d', '.braw', '.mxf', '.avi', '.mts', '.m2ts',
                '.m4v', '.insv', '.mkv', '.wmv', '.lrf']),
    ("ภาพ RAW", ['.cr3', '.cr2', '.nef', '.dng', '.arw', '.raf', '.orf', '.rw2', '.srw']),
    ("ภาพ", ['.jpg', '.jpeg', '.png', '.heic', '.tif', '.tiff', '.webp', '.psd',
             '.psb', '.gif']),
    ("เสียง", ['.wav', '.mp3', '.aif', '.aiff', '.m4a', '.flac', '.aac']),
    ("ไฟล์งาน/โปรเจกต์", ['.prproj', '.aep', '.drp', '.skp', '.dwg', '.indd', '.ai',
                          '.fcpxml', '.edl', '.aaf']),
    ("บีบอัด/อิมเมจ", ['.zip', '.rar', '.7z', '.dmg', '.iso', '.tar', '.gz']),
    ("เอกสาร", ['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.txt', '.csv']),
    ("cache/ชั่วคราว", ['.pek', '.cfa', '.lrprev', '.tmp', '.temp', '.bak', '.thm']),
]

_RECLAIM_CACHE = {}
_RECLAIM_CACHE_LOCK = threading.Lock()


def _reclaim_cache_key(conn):
    """Anything that changes the answer (a scan, a forget) moves last_scanned or
    the drive count, so this is enough to invalidate by itself."""
    row = conn.execute(
        "SELECT COALESCE(MAX(last_scanned), 0), COUNT(*) FROM drives").fetchone()
    return "%s:%s" % (row[0], row[1])


def _cached(conn, name, build):
    key = _reclaim_cache_key(conn) + "|" + name
    with _RECLAIM_CACHE_LOCK:
        if key in _RECLAIM_CACHE:
            return _RECLAIM_CACHE[key]
    value = build()
    with _RECLAIM_CACHE_LOCK:
        for stale in [k for k in _RECLAIM_CACHE if not k.startswith(key.split("|")[0])]:
            del _RECLAIM_CACHE[stale]     # drop everything from an older catalog state
        _RECLAIM_CACHE[key] = value
    return value


def _safe_date(mtime):
    """Formatted date, or None when the timestamp is impossible (see MTIME_FLOOR)."""
    if mtime is None or mtime < MTIME_FLOOR or mtime > time.time() + 86400:
        return None
    return time.strftime("%Y-%m-%d", time.localtime(mtime))


def reclaim_junk_summary(conn):
    """Per-rule file count + bytes. Measured on a 2M-row catalog: running the
    rules one at a time takes ~3.7s, folding them into a single pass with CASE
    WHEN takes ~5.7s - some rules ride the filename index, and one pass forces
    every predicate onto every row. So: one query per rule, and cache it."""
    def build():
        out = []
        for rule in RECLAIM_RULES:
            n, b = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM files WHERE " + rule["where"]
            ).fetchone()
            out.append({"id": rule["id"], "label": rule["label"], "tier": rule["tier"],
                        "note": rule["note"], "files": n, "bytes": b,
                        "bytes_human": human_size(b)})
        return out
    return _cached(conn, "junk", build)


def reclaim_junk_files(conn, rule_id, limit=500):
    rule = _RULES_BY_ID.get(rule_id)
    if rule is None:
        return None
    rows = conn.execute(
        "SELECT drive_label, relpath, size, mtime FROM files WHERE " + rule["where"] +
        " ORDER BY size DESC LIMIT ?", (limit,)).fetchall()
    return [{"drive": d, "path": p, "files": 1, "size": s,
             "size_human": human_size(s), "mdate": _safe_date(mt)}
            for d, p, s, mt in rows]


def reclaim_bigold(conn, min_bytes, years, drive=None, mode="file", limit=200):
    """Big files (or job folders) nobody has touched in a long time, ranked by how
    much space deleting them would actually return. Files with a broken mtime are
    excluded and reported separately - see reclaim_bad_mtime()."""
    now = time.time()
    cutoff = now - years * YEAR_SECONDS
    where_drive = " AND drive_label = ?" if drive else ""

    if mode == "folder":
        args = [MTIME_FLOOR, now + 86400, cutoff]
        if drive:
            args.append(drive)
        rows = conn.execute(
            "SELECT drive_label, depth1, COUNT(*), SUM(size), MAX(mtime) FROM files "
            "WHERE mtime BETWEEN ? AND ? AND mtime < ?" + where_drive +
            " GROUP BY drive_label, depth1 HAVING SUM(size) >= ? "
            "ORDER BY SUM(size) DESC LIMIT ?", args + [min_bytes, limit]).fetchall()
        return [{"drive": d, "path": f or "(root)", "files": n, "size": b,
                 "size_human": human_size(b), "mdate": _safe_date(newest),
                 "years": round((now - newest) / YEAR_SECONDS, 1)}
                for d, f, n, b, newest in rows]

    args = [MTIME_FLOOR, now + 86400, cutoff, min_bytes]
    if drive:
        args.append(drive)
    args += [now, float(YEAR_SECONDS), limit]
    rows = conn.execute(
        "SELECT drive_label, relpath, size, mtime FROM files "
        "WHERE mtime BETWEEN ? AND ? AND mtime < ? AND size >= ?" + where_drive +
        " ORDER BY size * (1.0 + (? - mtime) / ?) DESC LIMIT ?", args).fetchall()
    return [{"drive": d, "path": p, "files": 1, "size": s, "size_human": human_size(s),
             "mdate": _safe_date(mt), "years": round((now - mt) / YEAR_SECONDS, 1)}
            for d, p, s, mt in rows]


def reclaim_bigold_total(conn, min_bytes, years, drive=None, mode="file"):
    """Everything matching the filter, not just the page being displayed - the
    headline number has to be the real total or it quietly understates the win."""
    now = time.time()
    args = [MTIME_FLOOR, now + 86400, now - years * YEAR_SECONDS]
    where_drive = " AND drive_label = ?" if drive else ""
    if mode == "folder":
        if drive:
            args.append(drive)
        n, b = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(b), 0) FROM (SELECT SUM(size) b FROM files "
            "WHERE mtime BETWEEN ? AND ? AND mtime < ?" + where_drive +
            " GROUP BY drive_label, depth1 HAVING SUM(size) >= ?)",
            args + [min_bytes]).fetchone()
    else:
        args.append(min_bytes)
        if drive:
            args.append(drive)
        n, b = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM files "
            "WHERE mtime BETWEEN ? AND ? AND mtime < ? AND size >= ?" + where_drive,
            args).fetchone()
    return {"items": n, "bytes": b, "bytes_human": human_size(b)}


def reclaim_mark_duplicates(conn, rows):
    """Flag rows that have a same-name same-size copy on another drive - those are
    the safest ones to delete first. Same caveat as the duplicates section: this
    compares filename + size, never contents, so it means 'almost certainly the
    same file', not 'proven identical'."""
    for r in rows:
        if r.get("files", 1) != 1:
            continue
        n = conn.execute(
            "SELECT COUNT(DISTINCT drive_label) FROM files WHERE filename = ? AND size = ?",
            (os.path.basename(r["path"]), r["size"])).fetchone()[0]
        r["copies_elsewhere"] = max(0, n - 1)
    return rows


def reclaim_by_type(conn):
    """Where the space actually went, per drive and overall."""
    def build():
        bucket_of = {}
        for label, exts in RECLAIM_TYPES:
            for e in exts:
                bucket_of[e] = label
        per_drive, totals = {}, {}
        for drive, ext, n, b in conn.execute(
                "SELECT drive_label, ext, COUNT(*), SUM(size) FROM files "
                "GROUP BY drive_label, ext"):
            label = bucket_of.get((ext or "").lower(), "อื่นๆ")
            cur = per_drive.setdefault(drive, {}).setdefault(label, [0, 0])
            cur[0] += n
            cur[1] += b or 0
            t = totals.setdefault(label, [0, 0])
            t[0] += n
            t[1] += b or 0

        def pack(d):
            out = [{"label": k, "files": v[0], "bytes": v[1], "bytes_human": human_size(v[1])}
                   for k, v in d.items()]
            out.sort(key=lambda x: -x["bytes"])
            return out

        return {"total": pack(totals), "drives": {k: pack(v) for k, v in per_drive.items()}}
    return _cached(conn, "bytype", build)


def reclaim_bad_mtime(conn):
    """Files whose timestamp is impossible - they can't be judged by age, so they
    get their own bucket instead of silently skewing the rankings."""
    def build():
        n, b = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM files WHERE mtime < ? OR mtime > ?",
            (MTIME_FLOOR, time.time() + 86400)).fetchone()
        return {"files": n, "bytes": b, "bytes_human": human_size(b)}
    return _cached(conn, "badmtime", build)


def reclaim_markdown(title, rows, note=""):
    """Markdown checklist - tick items off while deleting by hand, and it drops
    straight into Obsidian next to the export-obsidian notes."""
    total = sum(r.get("size", 0) for r in rows)
    lines = ["# " + title, ""]
    if note:
        lines += ["> " + note, ""]
    lines += ["สร้างโดย HDDCAT %s · %s" % (__version__, time.strftime("%Y-%m-%d %H:%M")), "",
              "รวม %s รายการ · คืนพื้นที่ได้ ~%s" % (format(len(rows), ","), human_size(total)), ""]
    by_drive = {}
    for r in rows:
        by_drive.setdefault(r["drive"], []).append(r)
    for drive in sorted(by_drive):
        items = by_drive[drive]
        lines += ["## %s — %s" % (drive, human_size(sum(i.get("size", 0) for i in items))), ""]
        for r in items:
            extra = []
            if r.get("mdate"):
                extra.append("แก้ไขล่าสุด " + r["mdate"])
            if r.get("files", 1) > 1:
                extra.append("%s ไฟล์" % format(r["files"], ","))
            if r.get("copies_elsewhere"):
                extra.append("มีสำเนาอีก %d ไดรฟ์" % r["copies_elsewhere"])
            suffix = ("  _(%s)_" % " · ".join(extra)) if extra else ""
            lines.append("- [ ] `%s` — **%s**%s" % (r["path"], r["size_human"], suffix))
        lines.append("")
    lines += ["---", "path เป็นตำแหน่งภายในไดรฟ์นั้น · HDDCAT ไม่ลบไฟล์ให้ "
              "รายการนี้ไว้ตรวจแล้วลบเอง"]
    return "\n".join(lines) + "\n"


def cmd_reclaim(args):
    conn = get_conn(args.db)
    if args.junk or not (args.big_old or args.by_type):
        print("=== ของที่ลบได้ (cache/ไฟล์ขยะ) ===")
        total = 0
        for r in reclaim_junk_summary(conn):
            if not r["files"]:
                continue
            total += r["bytes"]
            tier = "ลบได้" if r["tier"] == "safe" else "ดูก่อน"
            print("  [%s] %-38s %8s ไฟล์  %9s"
                  % (tier, r["label"], format(r["files"], ","), r["bytes_human"]))
        print("  รวมคืนได้ ~%s\n" % human_size(total))
    if args.big_old:
        rows = reclaim_mark_duplicates(conn, reclaim_bigold(
            conn, int(args.min_gb * 1024 ** 3), args.years,
            mode="folder" if args.folders else "file", limit=args.limit))
        kind = "โฟลเดอร์" if args.folders else "ไฟล์"
        print("=== %sใหญ่ที่ไม่ถูกแตะเกิน %s ปี (>= %sGB) ==="
              % (kind, args.years, args.min_gb))
        for r in rows:
            dup = ("  [มีสำเนาอีก %d ไดรฟ์]" % r["copies_elsewhere"]) if r.get("copies_elsewhere") else ""
            print("  [%s] %s  %s (ล่าสุด %s)%s"
                  % (r["drive"], r["path"], r["size_human"], r["mdate"] or "?", dup))
        print("  รวม %d รายการ ~%s\n" % (len(rows), human_size(sum(r["size"] for r in rows))))
        if args.md:
            with open(args.md, "w", encoding="utf-8") as f:
                f.write(reclaim_markdown("%sใหญ่ที่ไม่ถูกแตะเกิน %s ปี" % (kind, args.years), rows))
            print("  เขียน checklist ไปที่ %s" % args.md)
    if args.by_type:
        print("=== พื้นที่หายไปไหน ===")
        for r in reclaim_by_type(conn)["total"]:
            print("  %-22s %9s ไฟล์  %9s"
                  % (r["label"], format(r["files"], ","), r["bytes_human"]))
        print()
    bad = reclaim_bad_mtime(conn)
    if bad["files"]:
        print("หมายเหตุ: มี %s ไฟล์ (%s) ที่วันที่แก้ไขเชื่อไม่ได้ (อนาคต/ก่อนปี 2000) "
              "- ไม่ถูกนับในรายการ 'ไม่ถูกแตะนาน'"
              % (format(bad["files"], ","), bad["bytes_human"]))
    conn.close()


def cmd_forget(args):
    conn = get_conn(args.db)
    if not args.yes:
        n = conn.execute("SELECT COUNT(*) FROM files WHERE drive_label=?", (args.label,)).fetchone()[0]
        print(f"จะลบ '{args.label}' ({n} ไฟล์) ออกจาก catalog - ไฟล์จริงบนไดรฟ์ไม่ถูกแตะ")
        print(f"ยืนยันด้วย: python3 catalog.py forget {args.label} --yes")
        return
    n = forget_drive(conn, args.label)
    if n is None:
        print(f"ไม่พบ drive '{args.label}' ใน catalog")
    else:
        print(f"ลบ '{args.label}' ออกจาก catalog แล้ว ({n} ไฟล์) - ไฟล์จริงไม่ถูกแตะ")


_DIST_README = """# HDDCAT 🐈💾 — Every File You Own. One Search Away.

สแกนฮาร์ดดิสก์ทุกลูกของคุณเข้า catalog เดียว ค้นเจอทุกไฟล์ในเสี้ยววินาที
โดยไม่ต้องเสียบไดรฟ์ — ข้อมูลทั้งหมดอยู่ในเครื่องคุณ 100% ไม่มี cloud

## วิธีเปิดใช้ (macOS)

1. แตกไฟล์ zip แล้วลาก HDDCAT.app ไปไว้ที่ไหนก็ได้ (เช่น โฟลเดอร์ Applications)
2. **คลิกขวา** ที่ HDDCAT.app แล้วเลือก **Open** (ครั้งแรกครั้งเดียว — macOS ถามยืนยัน
   เพราะแอปมาจากอินเทอร์เน็ต) ครั้งต่อไปดับเบิลคลิกได้เลย

   > ถ้าใช้ macOS 15 (Sequoia) ขึ้นไปแล้วคลิกขวาไม่มีผล: เปิด System Settings > Privacy & Security
   > เลื่อนลงล่างสุด จะเห็นข้อความว่า HDDCAT ถูกบล็อก ให้กด "Open Anyway" แล้วยืนยันอีกครั้ง —
   > ทำครั้งแรกครั้งเดียวเช่นกัน

3. HDDCAT เปิดเป็นหน้าต่างของตัวเอง (ไม่ใช้เบราว์เซอร์แล้วตั้งแต่ v1.2.0) — ปิดโปรแกรมด้วย ⌘Q
   หรือคลิกขวาที่ไอคอนแมวใน Dock แล้วเลือก Quit

> ครั้งแรก ถ้าเครื่องยังไม่มี python3 ระบบจะเด้งหน้าต่างชวนติดตั้ง
> "Command Line Developer Tools" — กด Install รอสักครู่ แล้วเปิดใหม่อีกครั้ง

## ข้อมูลอยู่ที่ไหน?

ทุกอย่างอยู่ในโฟลเดอร์ ~/HDDCAT (ไฟล์ catalog.db) — ไม่มีอะไรถูกส่งออกจากเครื่องคุณ
อยากย้ายเครื่องก็ก๊อปโฟลเดอร์นี้ไป

แอปเช็คเวอร์ชันใหม่จาก hddcat.tnmlab.dev วันละครั้ง (ส่งแค่หมายเลขเวอร์ชัน ปิดได้ในแถบแจ้งเตือน)

## ใช้จาก Terminal ก็ได้

    python3 catalog.py scan /Volumes/ไดรฟ์ของคุณ --label ชื่อไดรฟ์
    python3 catalog.py search คำค้น
    python3 catalog.py reclaim --big-old --md เคลียร์พื้นที่.md
    python3 catalog.py serve

---
MIT License · © 2026 [Touchnewmedia Co., Ltd.](https://www.thetnm.com)
GitHub: https://github.com/korakotcha06-dev/hddcat · ☕ https://www.buymeacoffee.com/korakot
"""

_DIST_LICENSE = """MIT License

Copyright (c) 2026 Touchnewmedia Co., Ltd.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

_DIST_INFO_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>HDDCAT</string>
    <key>CFBundleDisplayName</key>
    <string>HDDCAT</string>
    <key>CFBundleIdentifier</key>
    <string>dev.tnmlab.hddcat</string>
    <key>CFBundleVersion</key>
    <string>{version}</string>
    <key>CFBundleShortVersionString</key>
    <string>{version}</string>
    <key>CFBundleIconFile</key>
    <string>HDDCAT</string>
    <key>CFBundleExecutable</key>
    <string>HDDCAT</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSMinimumSystemVersion</key>
    <string>11.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
"""

# Fallback executable, used only when the native shell can't be compiled (no
# Xcode Command Line Tools). Opens the web UI in the user's default browser -
# what every release before 1.2.0 did. Keeps all user data under ~/HDDCAT (not
# wherever the .app happens to be dragged to - /Applications is typically not
# user-writable).
_DIST_APP_LAUNCHER = (
    '#!/bin/bash\n'
    'BUNDLE="$(cd "$(dirname "$0")/../.." && pwd)"\n'
    'mkdir -p "$HOME/HDDCAT"\n'
    'cd "$HOME/HDDCAT"\n'
    'exec /usr/bin/env python3 "$BUNDLE/Contents/Resources/catalog.py" serve\n'
)


def _build_app_shell(root):
    """Compile shell/HDDCATShell.swift - the native window HDDCAT.app opens.
    Returns its bytes, or None when the machine can't build it (no Command Line
    Tools), in which case the caller falls back to the old browser launcher."""
    script = os.path.join(root, "shell", "build-shell.sh")
    if not os.path.isfile(script):
        print("WARN: ไม่พบ shell/build-shell.sh - จะใช้ launcher แบบเปิดเบราว์เซอร์แทน")
        return None
    if shutil.which("swiftc") is None and subprocess.call(
            ["xcrun", "-f", "swiftc"], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL) != 0:
        print("WARN: ไม่พบ swiftc (ต้องมี Xcode Command Line Tools) - "
              "จะใช้ launcher แบบเปิดเบราว์เซอร์แทน")
        return None
    print("กำลัง build native shell (shell/build-shell.sh)…")
    if subprocess.call(["/bin/bash", script]) != 0:
        print("WARN: build shell ไม่ผ่าน - จะใช้ launcher แบบเปิดเบราว์เซอร์แทน")
        return None
    binary = os.path.join(root, "shell", "build", "HDDCAT")
    if not os.path.isfile(binary):
        print("WARN: build shell ไม่ได้ไฟล์ผลลัพธ์ - จะใช้ launcher แบบเปิดเบราว์เซอร์แทน")
        return None
    with open(binary, "rb") as f:
        return f.read()


def cmd_build_dist(args):
    """Build dist/HDDCAT.zip - a self-contained HDDCAT/ folder with a real,
    double-clickable HDDCAT.app (cat icon, shows in the Dock) plus a plain
    catalog.py copy for Terminal users, README and LICENSE. Never bundles
    catalog.db - that's per-machine/private.

    Since 1.2.0 the .app's executable is the compiled Swift shell, which opens
    the UI in its own window instead of the user's browser. If that can't be
    compiled here the zip falls back to the old bash launcher, which still
    works - it just opens a browser tab."""
    root = os.path.dirname(os.path.abspath(__file__))
    dist_dir = os.path.join(root, "dist")
    os.makedirs(dist_dir, exist_ok=True)
    zip_path = os.path.join(dist_dir, "HDDCAT.zip")
    now = time.localtime()[:6]

    icns_path = os.path.join(root, "icon-work", "HDDCAT.icns")
    if not os.path.isfile(icns_path):
        print(f"ERROR: ไม่พบไฟล์ไอคอน {icns_path}")
        print("สร้างก่อนด้วย qlmanage/iconutil (ดูขั้นตอนใน R11 ของแผน) แล้วค่อยรัน build-dist ใหม่")
        sys.exit(1)
    with open(icns_path, "rb") as f:
        icns_bytes = f.read()

    shell_bytes = _build_app_shell(root)

    plist = _DIST_INFO_PLIST.format(version=__version__)

    # The .app is assembled on disk first so codesign can sign it as a real
    # bundle. Signing only the executable leaves the bundle half-signed, which
    # macOS reports as "app is damaged" instead of the normal unidentified-
    # developer prompt.
    staging = tempfile.mkdtemp(prefix="hddcat-app-")
    app_dir = os.path.join(staging, "HDDCAT.app")
    macos_dir = os.path.join(app_dir, "Contents", "MacOS")
    res_dir = os.path.join(app_dir, "Contents", "Resources")
    os.makedirs(macos_dir)
    os.makedirs(res_dir)
    with open(os.path.join(app_dir, "Contents", "Info.plist"), "w", encoding="utf-8") as f:
        f.write(plist)
    exe_path = os.path.join(macos_dir, "HDDCAT")
    if shell_bytes:
        with open(exe_path, "wb") as f:
            f.write(shell_bytes)
    else:
        with open(exe_path, "w", encoding="utf-8") as f:
            f.write(_DIST_APP_LAUNCHER)
    os.chmod(exe_path, 0o755)
    with open(os.path.join(res_dir, "HDDCAT.icns"), "wb") as f:
        f.write(icns_bytes)
    shutil.copyfile(os.path.abspath(__file__), os.path.join(res_dir, "catalog.py"))
    # (1.2.0: no assets/ any more - the Home tab is a dashboard now, not a
    # landing page, so the 3 marketing photos it used to show are gone. The
    # GET /assets/<name> route stays for anyone theming their own copy.)

    signed = False
    if shell_bytes:
        # ad-hoc ("-") signature: free, no Apple Developer ID, and enough for
        # macOS on Apple Silicon to execute the binary at all
        if subprocess.call(["codesign", "--force", "--deep", "--sign", "-", app_dir],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0 and \
           subprocess.call(["codesign", "--verify", "--deep", "--strict", app_dir],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0:
            signed = True
        else:
            print("WARN: เซ็น .app (ad-hoc) ไม่สำเร็จ - แอปอาจเปิดไม่ได้บนเครื่อง Apple Silicon")

    entries = []
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # plain copy at the top level, for Terminal/CLI users
        zf.write(os.path.abspath(__file__), "HDDCAT/catalog.py")
        entries.append("HDDCAT/catalog.py")

        # the assembled (and signed) bundle, permissions preserved
        for dirpath, _dirnames, filenames in os.walk(app_dir):
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, staging)
                arc = "HDDCAT/" + rel.replace(os.sep, "/")
                zi = zipfile.ZipInfo(arc, date_time=now)
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.external_attr = (os.stat(full).st_mode & 0xFFFF) << 16
                with open(full, "rb") as f:
                    zf.writestr(zi, f.read())
                entries.append(arc)
        shutil.rmtree(staging, ignore_errors=True)

        zi = zipfile.ZipInfo("HDDCAT/README.md", date_time=now)
        zi.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(zi, _DIST_README)
        entries.append("HDDCAT/README.md")

        zi = zipfile.ZipInfo("HDDCAT/LICENSE", date_time=now)
        zi.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(zi, _DIST_LICENSE)
        entries.append("HDDCAT/LICENSE")

    size = os.path.getsize(zip_path)
    print(f"wrote {zip_path} ({human_size(size)})")
    print("  แอป: " + ("หน้าต่างของตัวเอง (native shell"
                       + (", เซ็น ad-hoc แล้ว)" if signed else ", ยังไม่ได้เซ็น)")
                       if shell_bytes else "เปิดผ่านเบราว์เซอร์ (fallback launcher)"))
    for e in entries:
        print(f"  {e}")


def files_in_folder(conn, drive, folder, limit=1000):
    """Files inside one Library row's bucket (web UI drill-down). Prefix-matches relpath
    with substr() - no LIKE, so %/_ in folder names can't break the match."""
    sep = os.sep
    if folder == "(root)":
        rows = conn.execute(
            "SELECT relpath, filename, ext, size, mtime FROM files "
            "WHERE drive_label=? AND depth1='' ORDER BY relpath LIMIT ?",
            (drive, limit)).fetchall()
    elif folder.endswith(sep + "(loose files)"):
        prefix = folder[:-len("(loose files)")]
        rows = conn.execute(
            "SELECT relpath, filename, ext, size, mtime FROM files "
            "WHERE drive_label=? AND substr(relpath,1,?)=? AND instr(substr(relpath,?),?)=0 "
            "ORDER BY relpath LIMIT ?",
            (drive, len(prefix), prefix, len(prefix) + 1, sep, limit)).fetchall()
    else:
        prefix = folder + sep
        rows = conn.execute(
            "SELECT relpath, filename, ext, size, mtime FROM files "
            "WHERE drive_label=? AND substr(relpath,1,?)=? ORDER BY relpath LIMIT ?",
            (drive, len(prefix), prefix, limit)).fetchall()
    return [{"relpath": r, "filename": fn, "ext": e, "size": s,
             "size_human": human_size(s),
             "mdate": time.strftime("%Y-%m-%d", time.localtime(mt))}
            for r, fn, e, s, mt in rows]


def cmd_export_obsidian(args):
    conn = get_conn(args.db)
    vault = args.vault_folder
    os.makedirs(vault, exist_ok=True)
    drives = conn.execute(
        "SELECT drive_label, total_bytes, free_bytes, last_scanned FROM drives ORDER BY drive_label"
    ).fetchall()
    for label, total_bytes, free_bytes, last_scanned in drives:
        n, s = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size),0) FROM files WHERE drive_label=?", (label,)
        ).fetchone()
        scanned_date = time.strftime("%Y-%m-%d", time.localtime(last_scanned))
        folders = conn.execute(
            "SELECT depth1, COUNT(*), SUM(size) FROM files WHERE drive_label=? AND depth1 != '' "
            "GROUP BY depth1 ORDER BY 3 DESC",
            (label,)
        ).fetchall()
        safe_label = re.sub(r'[\\/:*?"<>|]', '-', label)
        path = os.path.join(vault, f"HDD - {safe_label}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("---\n")
            f.write("type: hdd-catalog\n")
            f.write(f"drive_label: {label}\n")
            f.write(f"scanned: {scanned_date}\n")
            f.write(f"total_files: {n}\n")
            f.write(f"total_size_bytes: {s}\n")
            if total_bytes:
                f.write(f"disk_total_bytes: {total_bytes}\n")
                f.write(f"disk_free_bytes: {free_bytes}\n")
            f.write("---\n\n")
            f.write(f"# {label}\n\n")
            f.write(f"- Scanned: {scanned_date}\n")
            f.write(f"- Files cataloged: {n} ({human_size(s)})\n")
            if total_bytes:
                f.write(f"- Disk: {human_size(total_bytes)} total, {human_size(free_bytes)} free\n")
            f.write("\n## Top-level folders\n\n")
            f.write("| Folder | Files | Size |\n|---|---|---|\n")
            for depth1, cnt, size in folders:
                f.write(f"| {depth1} | {cnt} | {human_size(size)} |\n")
        print(f"wrote {path}")
    print(f"\nExported {len(drives)} drive notes to {vault}")


# ---------------------------------------------------------------------------
# serve: local web UI  (stdlib http.server only - binds 127.0.0.1, never exposed)
# Design tokens taken from the Flowbit template (:root vars) - override any of
# them by dropping a theme.css next to catalog.py (loaded after built-in styles).
# ---------------------------------------------------------------------------

_JOBS_LOCK = threading.Lock()
_JOBS = {"scan": {"status": "idle"}, "dedup": {"status": "idle"}, "update": {"status": "idle"}}


def _jobs_snapshot():
    with _JOBS_LOCK:
        return json.loads(json.dumps(_JOBS, ensure_ascii=False))


def _start_scan_job(db_path, path, label):
    with _JOBS_LOCK:
        if _JOBS["scan"].get("status") == "running":
            return False, "มี scan กำลังทำงานอยู่ - รอให้เสร็จก่อน"
        _JOBS["scan"] = {"status": "running", "path": path, "label": label,
                         "count": 0, "started": time.time()}

    def progress(c):
        with _JOBS_LOCK:
            _JOBS["scan"]["count"] = c

    def run():
        try:
            res = scan_drive(db_path, path, label, progress=progress)
            res["bytes_human"] = human_size(res["bytes"])
            if res["disk_total"]:
                res["disk_total_human"] = human_size(res["disk_total"])
                res["disk_free_human"] = human_size(res["disk_free"])
                res["pct_free"] = round(res["disk_free"] / res["disk_total"] * 100)
            with _JOBS_LOCK:
                _JOBS["scan"] = {"status": "done", "path": path, "label": label, **res}
        except Exception as e:
            with _JOBS_LOCK:
                _JOBS["scan"] = {"status": "error", "path": path, "label": label,
                                 "error": str(e)}

    threading.Thread(target=run, daemon=True).start()
    return True, None


def _start_dedup_job(db_path, min_size):
    with _JOBS_LOCK:
        if _JOBS["dedup"].get("status") == "running":
            return False, "dedup กำลังทำงานอยู่ - รอให้เสร็จก่อน"
        _JOBS["dedup"] = {"status": "running", "min_size": min_size, "started": time.time()}

    def run():
        try:
            conn = get_conn(db_path)
            groups, total_waste, ngroups = build_dedup(conn, min_size=min_size, max_groups=300)
            conn.close()
            with _JOBS_LOCK:
                _JOBS["dedup"] = {"status": "done", "min_size": min_size,
                                  "groups": groups, "group_count": ngroups,
                                  "total_waste": total_waste,
                                  "total_waste_human": human_size(total_waste)}
        except Exception as e:
            with _JOBS_LOCK:
                _JOBS["dedup"] = {"status": "error", "error": str(e)}

    threading.Thread(target=run, daemon=True).start()
    return True, None


# ---------------------------------------------------------------------------
# update check + self-update (.app only) - server-side, stdlib only.
# Settings live in update_check.json next to catalog.db (CWD) so the CLI and
# the .app (which cd's into ~/HDDCAT) both get one shared, user-visible file.
# ---------------------------------------------------------------------------

_UPDATE_URL = "https://hddcat.tnmlab.dev/version.json"
_UPDATE_SETTINGS_FILE = "update_check.json"
_update_state = {"checked_at": 0, "latest": None, "url": None, "notes": ""}


def _version_tuple(v):
    try:
        return tuple(int(x) for x in str(v).strip().split("."))
    except (ValueError, AttributeError):
        return (0,)


def _is_newer(latest, current):
    return _version_tuple(latest) > _version_tuple(current)


def _load_update_settings():
    path = os.path.join(os.getcwd(), _UPDATE_SETTINGS_FILE)
    if not os.path.isfile(path):
        settings = {"enabled": True}
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
        except OSError:
            pass
        return settings
    try:
        with open(path, encoding="utf-8") as f:
            settings = json.load(f)
        if not isinstance(settings, dict) or "enabled" not in settings:
            return {"enabled": True}
        return settings
    except (OSError, ValueError):
        return {"enabled": True}


def _save_update_settings(settings):
    path = os.path.join(os.getcwd(), _UPDATE_SETTINGS_FILE)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def check_update(force=False):
    """Check hddcat.tnmlab.dev/version.json for a newer release. Never raises -
    on any failure the previous cached state is left untouched. Respects the
    update_check.json 'enabled' toggle unless force=True."""
    settings = _load_update_settings()
    enabled = bool(settings.get("enabled", True))
    if not enabled and not force:
        st = dict(_update_state)
        st["enabled"] = enabled
        return st
    now = time.time()
    if not force and (now - _update_state["checked_at"] < 21600):
        st = dict(_update_state)
        st["enabled"] = enabled
        return st
    try:
        req = urllib.request.Request(_UPDATE_URL,
                                      headers={"User-Agent": f"HDDCAT/{__version__}"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        latest = str(data.get("version") or "").strip()
        if latest:
            _update_state["checked_at"] = now
            _update_state["latest"] = latest
            _update_state["url"] = data.get("url")
            _update_state["notes"] = data.get("notes") or ""
    except Exception:
        pass  # offline / DNS / timeout / bad json - keep old state, never raise
    st = dict(_update_state)
    st["enabled"] = enabled
    return st


def _start_update_job():
    this_file = os.path.abspath(__file__)
    if "/Contents/Resources/" not in this_file:
        return False, ("อัปเดตอัตโนมัติได้เฉพาะเวอร์ชัน .app "
                        "(สาย CLI ใช้ git pull/โหลด zip เอง)")
    with _JOBS_LOCK:
        if _JOBS.get("update", {}).get("status") == "running":
            return False, "กำลังอัปเดตอยู่ - รอให้เสร็จก่อน"
        _JOBS["update"] = {"status": "running", "started": time.time()}

    def run():
        old_backup = None
        bundle_root = None
        tmp_zip = None
        tmp_extract = None
        try:
            st = check_update(force=True)
            latest = st.get("latest")
            url = st.get("url")
            if not latest or not url or not _is_newer(latest, __version__):
                raise RuntimeError("ไม่มีเวอร์ชันใหม่ให้อัปเดต")

            resources_dir = os.path.dirname(this_file)     # .../Contents/Resources
            contents_dir = os.path.dirname(resources_dir)  # .../Contents
            bundle_root = os.path.dirname(contents_dir)    # .../HDDCAT.app

            fd, tmp_zip = tempfile.mkstemp(suffix=".zip", prefix="hddcat-update-")
            os.close(fd)
            req = urllib.request.Request(url, headers={"User-Agent": f"HDDCAT/{__version__}"})
            with urllib.request.urlopen(req, timeout=30) as resp, open(tmp_zip, "wb") as out:
                shutil.copyfileobj(resp, out)

            tmp_extract = tempfile.mkdtemp(prefix="hddcat-update-")
            with zipfile.ZipFile(tmp_zip) as zf:
                zf.extractall(tmp_extract)

            new_app_path = os.path.join(tmp_extract, "HDDCAT", "HDDCAT.app")
            new_catalog = os.path.join(new_app_path, "Contents", "Resources", "catalog.py")
            if not os.path.isfile(new_catalog):
                raise RuntimeError("ไฟล์ zip ใหม่ไม่มี HDDCAT.app ที่ถูกต้อง")

            # zipfile.extractall drops permission bits, so the extracted
            # executable comes out 0644 and macOS refuses to launch the app.
            # Put the exec bit back before swapping the bundle in.
            new_exe = os.path.join(new_app_path, "Contents", "MacOS", "HDDCAT")
            if not os.path.isfile(new_exe):
                raise RuntimeError("ไฟล์ zip ใหม่ไม่มีไฟล์รันของแอป")
            os.chmod(new_exe, 0o755)

            ts = time.strftime("%Y%m%d%H%M%S")
            old_backup = bundle_root + ".old-" + ts
            shutil.move(bundle_root, old_backup)
            try:
                shutil.move(new_app_path, bundle_root)
            except Exception:
                if os.path.isdir(old_backup) and not os.path.isdir(bundle_root):
                    shutil.move(old_backup, bundle_root)
                    old_backup = None
                raise

            try:
                shutil.rmtree(old_backup)  # best-effort - keep the backup if this fails
            except Exception:
                pass

            with _JOBS_LOCK:
                _JOBS["update"] = {"status": "done", "version": latest,
                                    "message": f"อัปเดตเป็น v{latest} แล้ว"}
        except Exception as e:
            try:
                if old_backup and os.path.isdir(old_backup) and bundle_root \
                        and not os.path.isdir(bundle_root):
                    shutil.move(old_backup, bundle_root)
            except Exception:
                pass
            with _JOBS_LOCK:
                _JOBS["update"] = {"status": "error", "error": str(e)}
        finally:
            try:
                if tmp_zip and os.path.isfile(tmp_zip):
                    os.remove(tmp_zip)
            except Exception:
                pass
            try:
                if tmp_extract and os.path.isdir(tmp_extract):
                    shutil.rmtree(tmp_extract)
            except Exception:
                pass

    threading.Thread(target=run, daemon=True).start()
    return True, None


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>HDDCAT — HDD Catalog</title>
<!-- fonts: same families as the Flowbit theme; page falls back to system fonts offline -->
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700&family=Inter:wght@400;500;600&family=Noto+Sans+Thai:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
/* ---- design tokens (from Flowbit template :root) - override via theme.css ---- */
:root {
  --color-primary: #6633EE;
  --color-secondary: #030015;
  --color-bg-1: #FBFCFD;
  --color-home-bg: #FFF8F3;
  --color-border: #FFDBBF;
  --color-heading-1: #05011C;
  --color-body-1: #404040;
  --color-title: #26262C;
  --color-title-nav: #26262C;
  --color-white: #fff;
  --color-success: #26CF4B;
  --color-danger: #FF0003;
  --color-warning: #FF8F3C;
  --color-info: #1BA2DB;
  --counter-title: #717383;
  --p-regular: 400; --p-medium: 500; --p-semi-bold: 600; --p-bold: 700;
  --transition: all 0.4s;
  --font-primary: "Outfit", "Noto Sans Thai", -apple-system, sans-serif;
  --font-secondary: "Inter", "Noto Sans Thai", -apple-system, sans-serif;
  --font-body: var(--font-secondary);
  --font-size-b1: 16px;
  --radius-btn: 34px;      /* .rts-btn pill */
  --radius-card: 14px;
  --shadow-card: 0 4px 24px rgba(5, 1, 28, 0.06);
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html { font-size: var(--font-size-b1); }
body {
  font-family: var(--font-body);
  background: var(--color-home-bg);
  color: var(--color-body-1);
  min-height: 100vh;
}
h1,h2,h3,h4 { font-family: var(--font-primary); color: var(--color-heading-1); }

/* ---- top bar ---- */
.topbar { position: sticky; top: 0; z-index: 10; padding: 10px 20px;
  background: transparent; }
.topbar-inner {
  max-width: 1280px; margin: 0 auto; height: 60px;
  display: flex; align-items: center; justify-content: space-between; gap: 16px;
  padding: 0 10px 0 22px;
  background: rgba(255, 255, 255, 0.68);
  backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px);
  border: 1px solid rgba(102, 51, 238, 0.10);
  border-radius: 16px;
  box-shadow: 0 8px 32px rgba(5, 1, 28, 0.06);
}
.brand { display: flex; align-items: center; gap: 10px; }
.brand-mark { width: 32px; height: 32px; border-radius: 9px;
  background: linear-gradient(135deg, var(--color-primary), #9B7BFF);
  display: inline-flex; align-items: center; justify-content: center;
  box-shadow: 0 4px 12px rgba(102, 51, 238, 0.35); }
.brand-word { font-family: var(--font-primary); font-size: 19px; font-weight: var(--p-medium);
  color: var(--color-heading-1); }
.brand-word b { font-weight: var(--p-bold); color: var(--color-primary); }
.tabs { display: flex; gap: 26px; }
.tab-btn { font-family: var(--font-primary); font-weight: var(--p-medium); font-size: 15px;
  color: var(--color-title-nav); background: transparent; border: none; padding: 8px 2px;
  cursor: pointer; position: relative; transition: var(--transition); }
.tab-btn:hover { color: var(--color-primary); }
.tab-btn.active { color: var(--color-primary); font-weight: var(--p-semi-bold); }
.tab-btn.active::after { content: ""; position: absolute; left: 50%; transform: translateX(-50%);
  bottom: -2px; width: 18px; height: 3px; border-radius: 3px; background: var(--color-primary); }
.topbar-right { display: flex; align-items: center; gap: 12px; }
.stat-chip { font-size: 12px; color: var(--color-title); white-space: nowrap;
  background: rgba(5, 1, 28, 0.045); border-radius: 20px; padding: 7px 14px; }
.stat-chip:empty { display: none; }
.btn-scan-top { padding: 10px 20px; font-size: 14px; gap: 8px; }
@media (max-width: 1100px) { .stat-chip { display: none; } }
@media (max-width: 900px) {
  .tabs { gap: 14px; overflow-x: auto; }
  .btn-scan-top { display: none; }
}

main { max-width: 1280px; margin: 0 auto; padding: 24px 28px 80px; }
section.view { display: none; }
section.view.active { display: block; }

/* ---- buttons (Flowbit .rts-btn style) ---- */
.btn {
  font-family: var(--font-primary); font-weight: var(--p-medium); font-size: 15px;
  letter-spacing: 0.4px; line-height: 1;
  display: inline-flex; align-items: center; gap: 10px;
  padding: 12px 24px; border-radius: var(--radius-btn);
  background: var(--color-primary); color: var(--color-white);
  border: 1px solid var(--color-primary); cursor: pointer;
  transition: var(--transition);
}
.btn:hover { opacity: 0.88; transform: translateY(-1px); }
.btn:disabled { opacity: 0.45; cursor: not-allowed; transform: none; }
.btn.btn-border {
  background: transparent; color: var(--color-primary);
  border: 1px solid rgba(102, 51, 238, 0.4);
}
.btn.btn-border:hover { background: rgba(102, 51, 238, 0.07); }
.btn.btn-border.on { background: var(--color-primary); color: var(--color-white); }

/* ---- controls ---- */
.controls { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin: 18px 0 14px; }
input[type=text], select {
  font-family: var(--font-secondary); font-size: 15px; color: var(--color-title);
  background: var(--color-white); border: 1px solid var(--color-border);
  border-radius: 10px; padding: 10px 14px; outline: none;
  transition: var(--transition);
}
input[type=text]:focus, select:focus { border-color: var(--color-primary); }
#q { min-width: 320px; flex: 1; max-width: 480px; }
.hint { font-size: 12.5px; color: var(--counter-title); }

/* ---- table ---- */
.tbl-wrap { background: var(--color-white); border: 1px solid var(--color-border);
  border-radius: var(--radius-card); box-shadow: var(--shadow-card); overflow: clip; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
thead th {
  font-family: var(--font-primary); font-weight: var(--p-semi-bold); font-size: 13px;
  text-align: left; color: var(--color-heading-1);
  background: var(--color-bg-1); border-bottom: 1px solid var(--color-border);
  padding: 10px 12px; white-space: nowrap; position: sticky; top: 80px;
}
tbody td { padding: 8px 12px; border-bottom: 1px solid rgba(255, 219, 191, 0.45);
  vertical-align: top; }
tbody tr.row { cursor: pointer; transition: background 0.15s; }
tbody tr.row:hover { background: rgba(102, 51, 238, 0.045); }
tbody tr.row.open { background: rgba(102, 51, 238, 0.07); }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.clip { max-width: 340px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.muted { color: var(--counter-title); }

.badge { display: inline-block; font-size: 11.5px; font-weight: var(--p-medium);
  border-radius: 20px; padding: 2px 10px; line-height: 1.5; }
.badge.job   { background: rgba(102, 51, 238, 0.10); color: var(--color-primary); }
.badge.asset { background: rgba(27, 162, 219, 0.12); color: var(--color-info); }
.badge.other { background: rgba(113, 115, 131, 0.12); color: var(--counter-title); }
.badge.drive { background: rgba(5, 1, 28, 0.06); color: var(--color-title); }
.badge.cal   { background: rgba(255, 143, 60, 0.14); color: var(--color-warning); }

tr.detail td { background: var(--color-bg-1); padding: 12px 20px 16px; }
.file-list { font-size: 13px; max-height: 340px; overflow: auto; }
.file-list table td { padding: 4px 10px; border-bottom: 1px dashed rgba(255,219,191,0.5); }

/* ---- drive cards ---- */
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 18px; margin-top: 18px; }
.card { background: var(--color-white); border: 1px solid var(--color-border);
  border-radius: var(--radius-card); box-shadow: var(--shadow-card); padding: 20px; }
.card.danger { border-color: var(--color-danger);
  box-shadow: 0 4px 24px rgba(255, 0, 3, 0.10); }
.card h3 { font-size: 19px; margin-bottom: 2px; }
.card .free-line { font-weight: var(--p-semi-bold); font-size: 14px; margin: 4px 0 8px; }
.card .free-line.ok { color: var(--color-success); }
.card .free-line.warn { color: var(--color-warning); }
.card .free-line.bad { color: var(--color-danger); }
.bar { height: 8px; border-radius: 6px; background: rgba(5, 1, 28, 0.07); overflow: hidden; }
.bar .fill { height: 100%; border-radius: 6px; background: var(--color-primary); }
.card.danger .bar .fill { background: var(--color-danger); }
.card .stats { font-size: 13.5px; color: var(--color-body-1); margin-top: 10px; line-height: 1.75; }
.card .scanned { font-size: 12px; color: var(--counter-title); margin-top: 8px; }
.card-del { display: block; width: fit-content; margin: 10px 0 0 auto; font-size: 12px;
  color: var(--counter-title); background: transparent; border: none; cursor: pointer; padding: 0; }
.card-del:hover { color: var(--color-danger); }
.card-del-confirm { display: flex; align-items: center; justify-content: flex-end; gap: 8px;
  margin-top: 10px; font-size: 12px; color: var(--counter-title); flex-wrap: wrap; }
.card-del-confirm .card-del-yes { font-size: 12px; padding: 5px 14px; border-radius: 20px;
  background: var(--color-danger); color: var(--color-white); border: none; cursor: pointer; }
.card-del-confirm .card-del-no { font-size: 12px; padding: 5px 14px; border-radius: 20px;
  background: transparent; color: var(--color-title); border: 1px solid var(--color-border);
  cursor: pointer; }
.card-del-err { color: var(--color-danger); font-size: 12px; }

/* ---- home dashboard ---- */
.home-head { display: flex; align-items: flex-end; justify-content: space-between;
  gap: 16px; flex-wrap: wrap; margin-bottom: 16px; }
.home-title { font-family: var(--font-primary); font-weight: var(--p-bold);
  font-size: clamp(21px, 2.4vw, 26px); color: var(--color-heading-1); }
.home-sub { font-size: 13px; color: var(--counter-title); margin-top: 3px; }

.searchbox { position: relative; }
.searchbox .sb-icon { position: absolute; left: 19px; top: 50%; transform: translateY(-50%);
  pointer-events: none; color: var(--counter-title); display: flex; }
.searchbox input { width: 100%; font-family: inherit; font-size: 15.5px;
  color: var(--color-heading-1); background: var(--color-white);
  border: 1px solid var(--color-border); border-radius: var(--radius-btn);
  padding: 15px 20px 15px 50px; box-shadow: var(--shadow-card); outline: none; }
.searchbox input::placeholder { color: var(--counter-title); }
.searchbox input:focus { border-color: var(--color-primary); }
#home-hits { margin-top: 14px; }
#home-hits .dedup-head { margin-top: 0; }
#home-hits tr { cursor: pointer; }

.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 14px; margin: 20px 0 16px; }
.kpi { background: var(--color-white); border: 1px solid var(--color-border);
  border-radius: var(--radius-card); box-shadow: var(--shadow-card); padding: 17px 20px; }
.kpi-num { font-family: var(--font-primary); font-weight: var(--p-bold);
  font-size: 27px; line-height: 1.15; color: var(--color-heading-1); }
.kpi-num.small { font-size: 19px; padding-top: 6px; }
.kpi-label { font-size: 12.5px; color: var(--counter-title); margin-top: 5px; }

.home-cols { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; align-items: start; }
@media (max-width: 900px) { .home-cols { grid-template-columns: 1fr; } }
.home-panel { background: var(--color-white); border: 1px solid var(--color-border);
  border-radius: var(--radius-card); box-shadow: var(--shadow-card); padding: 19px 22px 12px; }
.home-panel h2 { font-family: var(--font-primary); font-weight: var(--p-semi-bold);
  font-size: 15.5px; margin-bottom: 10px; display: flex; align-items: center; gap: 9px; }
.home-panel h2 .pill { font-family: var(--font-secondary); font-size: 11px;
  font-weight: var(--p-semi-bold); color: var(--color-primary);
  background: rgba(102, 51, 238, 0.09); border-radius: 20px; padding: 3px 9px; }
.home-panel h2 .pill.alert { color: var(--color-danger); background: rgba(255, 0, 3, 0.08); }

.mini-row { display: flex; align-items: center; gap: 12px; padding: 11px 0;
  border-top: 1px solid rgba(5, 1, 28, 0.06); }
.mini-row.click { cursor: pointer; }
.mini-row.click:hover .mini-name { color: var(--color-primary); }
.mini-row:first-of-type { border-top: none; }
.mini-name { flex: 1; min-width: 0; font-weight: var(--p-medium); font-size: 13.5px;
  color: var(--color-title); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mini-meta { font-size: 12px; color: var(--counter-title); white-space: nowrap; }
.mini-meta.bad { color: var(--color-danger); font-weight: var(--p-semi-bold); }
.mini-meta.warn { color: var(--color-warning); font-weight: var(--p-medium); }
.mini-bar { width: 74px; height: 6px; border-radius: 3px; flex: none;
  background: rgba(5, 1, 28, 0.08); overflow: hidden; }
.mini-bar i { display: block; height: 100%; background: var(--color-primary); }
.mini-bar.warn i { background: var(--color-warning); }
.mini-bar.bad i { background: var(--color-danger); }
.home-note { font-size: 12.5px; color: var(--counter-title); padding: 4px 0 10px; }

.home-actions { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 18px; }

/* first run - nothing scanned yet */
.home-first { max-width: 560px; margin: 8vh auto; text-align: center; }
.home-first .cat { color: var(--color-primary); opacity: .9; }
.home-first h1 { font-family: var(--font-primary); font-weight: var(--p-bold);
  font-size: 25px; margin: 14px 0 8px; color: var(--color-heading-1); }
.home-first p { font-size: 14px; color: var(--counter-title); line-height: 1.75; margin-bottom: 22px; }

/* ---- reclaim space tab ---- */
.rc-hero { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
  gap: 14px; margin-bottom: 20px; }
.rc-hero .kpi-num { color: var(--color-primary); }
.rc-sec { margin-bottom: 26px; }
.rc-sec > h2 { font-family: var(--font-primary); font-weight: var(--p-semi-bold);
  font-size: 18px; margin-bottom: 4px; }
.rc-sec > .desc { font-size: 13px; color: var(--counter-title); margin-bottom: 12px;
  line-height: 1.7; }
.rc-card { background: var(--color-white); border: 1px solid var(--color-border);
  border-radius: var(--radius-card); box-shadow: var(--shadow-card); overflow: hidden; }
.rc-rule { display: flex; align-items: center; gap: 12px; padding: 13px 18px;
  border-top: 1px solid rgba(5, 1, 28, 0.06); cursor: pointer; }
.rc-rule:first-child { border-top: none; }
.rc-rule:hover { background: rgba(102, 51, 238, 0.03); }
.rc-rule-main { flex: 1; min-width: 0; }
.rc-rule-label { font-weight: var(--p-medium); font-size: 14px; color: var(--color-title); }
.rc-rule-note { font-size: 12px; color: var(--counter-title); margin-top: 2px; }
.rc-rule-size { font-family: var(--font-primary); font-weight: var(--p-semi-bold);
  font-size: 15px; color: var(--color-heading-1); white-space: nowrap; }
.rc-rule-files { font-size: 12px; color: var(--counter-title); white-space: nowrap;
  min-width: 92px; text-align: right; }
.tier { font-size: 11px; font-weight: var(--p-semi-bold); border-radius: 20px;
  padding: 3px 10px; white-space: nowrap; }
.tier.safe { color: #0f7b33; background: rgba(38, 207, 75, 0.14); }
.tier.review { color: #9a5b00; background: rgba(255, 143, 60, 0.16); }
.rc-detail { padding: 0 18px 16px; }
.rc-actions { display: flex; gap: 10px; flex-wrap: wrap; margin: 12px 0 0; }
.rc-actions .btn { padding: 8px 18px; font-size: 13px; }
.rc-actions a.btn { text-decoration: none; display: inline-flex; align-items: center; }
.rc-filters { display: flex; gap: 10px; flex-wrap: wrap; align-items: center;
  margin-bottom: 12px; }
.rc-filters select, .rc-filters input { font-family: inherit; font-size: 13.5px;
  color: var(--color-title); background: var(--color-white);
  border: 1px solid var(--color-border); border-radius: var(--radius-btn);
  padding: 9px 14px; outline: none; }
.rc-filters label { font-size: 13px; color: var(--counter-title); }
.rc-toggle { display: inline-flex; border: 1px solid var(--color-border);
  border-radius: var(--radius-btn); overflow: hidden; }
.rc-toggle button { font-family: inherit; font-size: 13px; padding: 9px 16px;
  border: none; background: var(--color-white); color: var(--color-title); cursor: pointer; }
.rc-toggle button.on { background: var(--color-primary); color: var(--color-white); }
.rc-badge-dup { font-size: 11px; font-weight: var(--p-semi-bold); color: var(--color-primary);
  background: rgba(102, 51, 238, 0.09); border-radius: 20px; padding: 2px 8px;
  white-space: nowrap; }
.rc-bar { display: flex; align-items: center; gap: 12px; padding: 9px 0;
  border-top: 1px solid rgba(5, 1, 28, 0.06); }
.rc-bar:first-child { border-top: none; }
.rc-bar-name { width: 150px; font-size: 13px; color: var(--color-title); flex: none; }
.rc-bar-track { flex: 1; height: 9px; border-radius: 5px; background: rgba(5, 1, 28, 0.07);
  overflow: hidden; }
.rc-bar-track i { display: block; height: 100%; background: var(--color-primary); }
.rc-bar-val { width: 150px; text-align: right; font-size: 12.5px; color: var(--counter-title);
  flex: none; white-space: nowrap; }
.rc-warn { font-size: 12.5px; color: var(--counter-title); background: rgba(255, 143, 60, .10);
  border: 1px solid rgba(255, 143, 60, .3); border-radius: 10px; padding: 11px 15px;
  line-height: 1.7; margin-bottom: 16px; }
.rc-copied { font-size: 12.5px; color: var(--color-success); align-self: center; }

/* ---- panels (scan / dedup) ---- */

.panel { background: var(--color-white); border: 1px solid var(--color-border);
  border-radius: var(--radius-card); box-shadow: var(--shadow-card);
  padding: 24px; margin-top: 18px; max-width: 760px; }
.panel h2 { font-size: 22px; margin-bottom: 6px; }
.panel p.desc { font-size: 13.5px; color: var(--counter-title); margin-bottom: 16px; }
.form-row { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 12px; }
.form-row input[type=text] { flex: 1; min-width: 240px; }
.progress-box { margin-top: 16px; padding: 14px 18px; border-radius: 10px;
  background: var(--color-bg-1); border: 1px dashed var(--color-border);
  font-size: 14px; display: none; }
.progress-box.show { display: block; }
.progress-box .big { font-family: var(--font-primary); font-size: 20px;
  font-weight: var(--p-semi-bold); color: var(--color-primary); }
.progress-box.err { border-color: var(--color-danger); color: var(--color-danger); }
/* ---- cat loader (replaces old .spin) ---- */
.cat-loader { position: relative; display: inline-block; color: var(--color-primary);
  vertical-align: -4px; margin-right: 6px; }
.cat-loader svg { display: block; animation: breathe 2.2s ease-in-out infinite;
  transform-origin: 50% 100%; }
@keyframes breathe { 0%, 100% { transform: scale(1, 1); } 50% { transform: scale(1.02, .95); } }
.zz { position: absolute; font-weight: 700; color: var(--color-primary); opacity: 0;
  animation: zfloat 2s infinite; line-height: 1; }
@keyframes zfloat { 0% { opacity: 0; transform: translateY(5px); } 25% { opacity: .9; }
  70% { opacity: .4; } 100% { opacity: 0; transform: translateY(-14px); } }
.big .z1 { font-size: 14px; right: -4px;  top: -4px;  animation-delay: 0s; }
.big .z2 { font-size: 18px; right: -16px; top: -10px; animation-delay: .65s; }
.big .z3 { font-size: 23px; right: -30px; top: -16px; animation-delay: 1.3s; }
.small .z1 { font-size: 8px;  right: -2px; top: -3px; animation-delay: 0s; }
.small .z2 { font-size: 10px; right: -9px; top: -6px; animation-delay: .65s; }
.small .z3 { font-size: 12px; right: -17px; top: -9px; animation-delay: 1.3s; }
.cat-loader.big { display: block; width: fit-content; margin: 0 auto 12px; vertical-align: initial; }

/* ---- dedup results ---- */
.dedup-head { font-family: var(--font-primary); font-size: 17px;
  font-weight: var(--p-semi-bold); color: var(--color-heading-1); margin: 20px 0 10px; }
.dedup-head .save { color: var(--color-success); }
details.dgroup { background: var(--color-white); border: 1px solid var(--color-border);
  border-radius: 10px; margin-bottom: 8px; overflow: hidden; }
details.dgroup summary { cursor: pointer; padding: 10px 16px; font-size: 14px;
  display: flex; gap: 12px; align-items: baseline; flex-wrap: wrap; }
details.dgroup summary::marker { color: var(--color-primary); }
details.dgroup .fname { font-weight: var(--p-medium); color: var(--color-title);
  overflow-wrap: anywhere; }
details.dgroup .meta { font-size: 12.5px; color: var(--counter-title); white-space: nowrap; }
details.dgroup .waste { color: var(--color-success); font-weight: var(--p-medium); }
details.dgroup ul { list-style: none; padding: 4px 18px 12px; font-size: 13px; }
details.dgroup li { padding: 3px 0; overflow-wrap: anywhere; }

/* ---- drive plug-in toasts ---- */
#drive-toasts { position: fixed; top: 92px; right: 20px; z-index: 50;
  display: flex; flex-direction: column; gap: 10px; max-width: 360px; }
.toast { background: var(--color-white); border: 1px solid var(--color-primary);
  border-radius: var(--radius-card); box-shadow: var(--shadow-card);
  padding: 14px 16px; animation: toast-in 0.3s ease-out; }
@keyframes toast-in { from { opacity: 0; transform: translateX(30px); }
  to { opacity: 1; transform: none; } }
.toast .t-title { font-family: var(--font-primary); font-weight: var(--p-semi-bold);
  color: var(--color-heading-1); font-size: 15px; margin-bottom: 2px;
  overflow-wrap: anywhere; }
.toast .t-sub { font-size: 12.5px; color: var(--counter-title); margin-bottom: 10px; }
.toast .t-actions { display: flex; gap: 8px; }
.toast .btn { padding: 8px 18px; font-size: 14px; }

#file-hits { margin-top: 22px; }
#file-hits .dedup-head { margin-top: 0; }
.readonly-note { font-size: 12.5px; color: var(--counter-title); margin-top: 14px; }

/* ---- update banner (docked under the topbar) ---- */
.update-banner { display: flex; align-items: center; gap: 14px; padding: 10px 24px;
  background: rgba(102, 51, 238, 0.08); border-bottom: 1px solid rgba(102, 51, 238, 0.18); }
.update-banner[hidden] { display: none; }
.update-banner .ub-icon { font-size: 18px; }
.update-banner .ub-text { flex: 1; font-size: 13.5px; color: var(--color-heading-1); }
.update-banner .ub-actions { display: flex; align-items: center; gap: 12px; flex-shrink: 0; }
.update-banner .ub-actions .btn { padding: 7px 16px; font-size: 13px; }
.update-banner .ub-skip { font-size: 12.5px; color: var(--counter-title);
  text-decoration: underline; cursor: pointer; white-space: nowrap; }

/* ---- version chip (persistent, all tabs) ---- */
#version-chip { position: fixed; bottom: 14px; right: 16px; z-index: 40;
  display: inline-flex; align-items: center; gap: 6px;
  font-family: inherit; font-size: 11.5px; color: var(--counter-title);
  background: rgba(255,255,255,.8); border: 1px solid var(--color-border);
  border-radius: 20px; padding: 6px 12px; cursor: pointer;
  -webkit-backdrop-filter: blur(6px); backdrop-filter: blur(6px); }
#version-chip:hover { border-color: var(--color-primary); color: var(--color-primary); }
#version-chip.vc-available { color: var(--color-primary); border-color: var(--color-primary); }
</style>
<link rel="stylesheet" href="/theme.css">
</head>
<body>

<header class="topbar">
  <div class="topbar-inner">
    <div class="brand">
      <span class="brand-mark" aria-hidden="true">
        <svg width="22" height="16" viewBox="0 0 32 22" fill="#fff"><rect x="2" y="16.5" width="28" height="3.5" rx="1.75" opacity=".45"/><path d="M6.3 16.5c-1.9 0-3.3-1.3-3.3-3 0-.9.6-1.6 1.4-1.9C5.3 8 8.6 5.4 12.6 5.4c2.3 0 4.4.85 5.9 2.2l.5-2.3c.07-.33.5-.42.7-.15l1.6 2.1c.5-.1 1-.1 1.5-.02l1.7-2c.22-.26.64-.15.7.18l.4 2.35c1.3.9 2.1 2.3 2.1 3.9 0 1.4-.63 2.68-1.66 3.58.04.15.06.3.06.47 0 .95-.83 1.72-1.85 1.72H6.3z"/></svg>
      </span>
      <span class="brand-word">HDD<b>CAT</b></span>
    </div>
    <nav class="tabs">
      <button class="tab-btn active" data-tab="home">หน้าแรก</button>
      <button class="tab-btn" data-tab="library">คลังงาน</button>
      <button class="tab-btn" data-tab="drives">ไดรฟ์</button>
      <button class="tab-btn" data-tab="scan">สแกน</button>
      <button class="tab-btn" data-tab="reclaim">เคลียร์พื้นที่</button>
    </nav>
    <div class="topbar-right">
      <span class="stat-chip" id="db-info"></span>
      <button class="btn btn-border btn-scan-top" id="topbar-scan">
        <span class="icon"><svg width="19" height="8" viewBox="0 0 19 8" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M17.9 0.900391L0.900024 0.900391" stroke="#6633EE" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/><path d="M17.9031 0.900781L11.8531 6.92578" stroke="#6633EE" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg></span>
        สแกนไดรฟ์
      </button>
    </div>
  </div>
</header>
<div id="update-banner" class="update-banner" hidden>
  <span class="ub-icon" aria-hidden="true">🐈</span>
  <span class="ub-text" id="ub-text"></span>
  <span class="ub-actions" id="ub-actions">
    <button class="btn" id="ub-apply">อัปเดตเลย</button>
    <button class="btn btn-border" id="ub-dismiss">ปิด</button>
    <a href="#" class="ub-skip" id="ub-skip">ไม่ต้องเช็คอีก</a>
  </span>
</div>
<div id="drive-toasts"></div>
<button id="version-chip" title="กดเพื่อเช็คอัปเดต">HDDCAT v{{VERSION}} · เช็คอัปเดต</button>

<main>

<!-- ================= HOME (app dashboard) ================= -->
<section class="view active" id="view-home">

  <!-- first run: no drives in the catalog yet -->
  <div class="home-first" id="home-first" hidden>
    <div class="cat" id="home-first-cat"></div>
    <h1>ยังไม่มีไดรฟ์ในคลัง</h1>
    <p>สแกนไดรฟ์แรกเพื่อเริ่มต้น — HDDCAT จะจำว่าไฟล์ไหนอยู่ไดรฟ์ลูกไหน<br>
       แล้วค้นเจอได้แม้ไดรฟ์จะถอดไปวางบนชั้นแล้ว</p>
    <button class="btn" id="home-first-scan">สแกนไดรฟ์แรก</button>
  </div>

  <div id="home-dash" hidden>
    <div class="home-head">
      <div>
        <div class="home-title">คลังไฟล์ของคุณ</div>
        <div class="home-sub" id="home-sub">กำลังโหลด…</div>
      </div>
      <div class="home-actions" style="margin-top:0">
        <button class="btn btn-border" id="home-go-library">เปิดคลังงาน</button>
        <button class="btn btn-border" id="home-go-dedup">เคลียร์พื้นที่</button>
        <button class="btn" id="home-go-scan">สแกนไดรฟ์</button>
      </div>
    </div>

    <div class="searchbox">
      <span class="sb-icon"><svg width="18" height="18" viewBox="0 0 24 24" fill="none"
        stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg></span>
      <input type="text" id="home-q" autocomplete="off" spellcheck="false"
             placeholder="ค้นหาไฟล์จากทุกไดรฟ์ — พิมพ์ชื่อไฟล์อย่างน้อย 2 ตัวอักษร">
    </div>
    <div id="home-hits"></div>

    <div class="kpis">
      <div class="kpi"><div class="kpi-num" id="kpi-drives">0</div>
        <div class="kpi-label">ไดรฟ์ในคลัง</div></div>
      <div class="kpi"><div class="kpi-num" id="kpi-files">0</div>
        <div class="kpi-label">ไฟล์ที่ค้นได้</div></div>
      <div class="kpi"><div class="kpi-num" id="kpi-size">0B</div>
        <div class="kpi-label">ขนาดรวมในคลัง</div></div>
      <div class="kpi"><div class="kpi-num small" id="kpi-last">—</div>
        <div class="kpi-label">สแกนล่าสุด</div></div>
    </div>

    <div class="home-cols">
      <div class="home-panel">
        <h2>พื้นที่ไดรฟ์ <span class="pill" id="home-space-pill" hidden></span></h2>
        <div id="home-space"></div>
      </div>
      <div class="home-panel">
        <h2>สแกนล่าสุด</h2>
        <div id="home-recent"></div>
      </div>
    </div>
  </div>
</section>

<!-- ================= LIBRARY ================= -->
<section class="view" id="view-library">
  <div class="controls">
    <input type="text" id="q" placeholder="ค้นหา client / งาน / ชื่อไฟล์ ..." autocomplete="off">
    <select id="f-drive"><option value="">ทุกไดรฟ์</option></select>
    <select id="f-type">
      <option value="">ทุกประเภท</option>
      <option value="job">job (งาน)</option>
      <option value="asset">asset (ใช้ร่วม)</option>
      <option value="other">other</option>
    </select>
    <button class="btn btn-border on" id="sort-client">เรียง client→วันที่</button>
    <button class="btn btn-border" id="sort-size">เรียงตามขนาด</button>
    <span class="hint" id="lib-stats"></span>
  </div>
  <div class="tbl-wrap">
    <table>
      <thead><tr>
        <th>ไดรฟ์</th><th>ประเภท</th><th>client</th><th>งาน</th>
        <th>วันที่</th><th class="num">ปีไฟล์</th>
        <th class="num">ไฟล์</th><th class="num">ขนาด</th>
      </tr></thead>
      <tbody id="lib-body"><tr><td colspan="8" class="muted">กำลังโหลดข้อมูล...</td></tr></tbody>
    </table>
  </div>
  <div id="file-hits"></div>
</section>

<!-- ================= DRIVES ================= -->
<section class="view" id="view-drives">
  <div class="cards" id="drive-cards"></div>
</section>

<!-- ================= SCAN ================= -->
<section class="view" id="view-scan">
  <div class="panel">
    <h2>สแกนไดรฟ์</h2>
    <p class="desc">เสียบ HDD แล้วใส่ path (เช่น /Volumes/WD-4TB-01) กับ label ประจำลูก
      (label เดิม = ทับข้อมูลเก่าของลูกนั้น)</p>
    <div class="form-row">
      <input type="text" id="scan-path" list="volumes" placeholder="/Volumes/..." autocomplete="off">
      <datalist id="volumes"></datalist>
    </div>
    <div class="form-row">
      <input type="text" id="scan-label" placeholder="label เช่น WD-4TB-01" autocomplete="off">
      <button class="btn" id="scan-btn">เริ่มสแกน</button>
    </div>
    <div class="progress-box" id="scan-progress"></div>
  </div>
</section>

<!-- ================= RECLAIM SPACE ================= -->
<section class="view" id="view-reclaim">

  <div class="rc-warn">
    HDDCAT <b>ไม่ลบไฟล์ให้</b> — หน้านี้ชี้เป้าและทำรายการให้เอาไปตรวจแล้วลบเอง
    ทุกอย่างคำนวณจากข้อมูลใน catalog ไม่ได้เปิดอ่านไฟล์จริง
    <span id="rc-stale"></span>
  </div>

  <div class="rc-hero">
    <div class="kpi"><div class="kpi-num" id="rc-kpi-junk">—</div>
      <div class="kpi-label">ลบ cache/ขยะได้</div></div>
    <div class="kpi"><div class="kpi-num" id="rc-kpi-big">—</div>
      <div class="kpi-label">ไฟล์ใหญ่ที่ไม่ถูกแตะนาน</div></div>
    <div class="kpi"><div class="kpi-num small" id="rc-kpi-top">—</div>
      <div class="kpi-label">ชนิดไฟล์ที่กินที่สุด</div></div>
  </div>

  <!-- 1. junk -->
  <div class="rc-sec">
    <h2>ของที่ลบได้ (cache / ไฟล์ขยะ)</h2>
    <p class="desc">โปรแกรมตัดต่อสร้างไฟล์พวกนี้ขึ้นมาใหม่ได้เอง —
      <span class="tier safe">ลบได้</span> คือสร้างใหม่ได้แน่นอน ·
      <span class="tier review">ดูก่อน</span> คือหน้าตาเหมือนขยะแต่ลบแล้วมีอะไรหาย
      <br>หมายเหตุ: <code>.DS_Store</code> / <code>Thumbs.db</code> ไม่อยู่ในนี้
      เพราะ HDDCAT ข้ามไม่เก็บเข้า catalog ตั้งแต่ตอนสแกน</p>
    <div class="rc-card" id="rc-junk"></div>
  </div>

  <!-- 2. big + old -->
  <div class="rc-sec">
    <h2>ใหญ่และไม่ได้แตะมานาน</h2>
    <p class="desc">เรียงตาม “ลบแล้วคืนพื้นที่ได้เท่าไหร่” (ขนาด × อายุ) —
      ไฟล์ที่วันที่แก้ไขเชื่อไม่ได้จะถูกกันออกจากรายการนี้</p>
    <div class="rc-filters">
      <div class="rc-toggle">
        <button id="rc-mode-file" class="on">รายไฟล์</button>
        <button id="rc-mode-folder">รายโฟลเดอร์งาน</button>
      </div>
      <label>ขนาดอย่างน้อย</label>
      <select id="rc-gb">
        <option value="0.5">500 MB</option>
        <option value="1" selected>1 GB</option>
        <option value="5">5 GB</option>
        <option value="50">50 GB</option>
      </select>
      <label>ไม่ถูกแตะเกิน</label>
      <select id="rc-years">
        <option value="1">1 ปี</option>
        <option value="2">2 ปี</option>
        <option value="3" selected>3 ปี</option>
        <option value="5">5 ปี</option>
      </select>
      <select id="rc-drive"><option value="">ทุกไดรฟ์</option></select>
    </div>
    <div id="rc-big"></div>
  </div>

  <!-- 3. where did the space go -->
  <div class="rc-sec">
    <h2>พื้นที่หายไปไหน</h2>
    <p class="desc">แยกตามชนิดไฟล์ เลือกดูเฉพาะไดรฟ์ได้</p>
    <div class="rc-filters">
      <select id="rc-type-drive"><option value="">ทุกไดรฟ์รวมกัน</option></select>
    </div>
    <div class="rc-card" style="padding: 8px 18px 14px" id="rc-bytype"></div>
  </div>

  <!-- 4. duplicates (moved in from the old tab) -->
  <div class="rc-sec">
    <h2>ไฟล์ซ้ำข้ามไดรฟ์</h2>
    <p class="desc">เทียบจากชื่อไฟล์ + ขนาดที่ตรงกันเป๊ะใน catalog (ไม่ได้อ่านเนื้อไฟล์
      เพราะไดรฟ์อาจไม่ได้เสียบอยู่) — แปลว่า “แทบจะแน่ใจว่าไฟล์เดียวกัน” ไม่ใช่ “พิสูจน์แล้ว”</p>
    <div class="panel" style="margin-top: 0">
      <div class="form-row">
        <select id="dedup-min">
          <option value="1048576">ขนาด ≥ 1 MB</option>
          <option value="10485760" selected>ขนาด ≥ 10 MB</option>
          <option value="104857600">ขนาด ≥ 100 MB</option>
          <option value="1073741824">ขนาด ≥ 1 GB</option>
        </select>
        <button class="btn" id="dedup-btn">ค้นหาไฟล์ซ้ำ</button>
      </div>
      <div class="progress-box" id="dedup-progress"></div>
    </div>
    <div id="dedup-results"></div>
  </div>
</section>

</main>

<script>
"use strict";
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const NO_CLIENT = "(no client)";
const HDDCAT_VERSION = "{{VERSION}}";
const api = async (path, opts) => {
  const r = await fetch(path, opts);
  return r.json();
};

/* ---------- cat loader (replaces old .spin) ---------- */
const CAT_SM = '<span class="cat-loader small"><svg width="26" height="18" viewBox="0 0 32 22" fill="currentColor"><rect x="2" y="16.5" width="28" height="3.5" rx="1.75" opacity=".35"/><path d="M6.3 16.5c-1.9 0-3.3-1.3-3.3-3 0-.9.6-1.6 1.4-1.9C5.3 8 8.6 5.4 12.6 5.4c2.3 0 4.4.85 5.9 2.2l.5-2.3c.07-.33.5-.42.7-.15l1.6 2.1c.5-.1 1-.1 1.5-.02l1.7-2c.22-.26.64-.15.7.18l.4 2.35c1.3.9 2.1 2.3 2.1 3.9 0 1.4-.63 2.68-1.66 3.58.04.15.06.3.06.47 0 .95-.83 1.72-1.85 1.72H6.3z"/></svg><span class="zz z1">z</span><span class="zz z2">z</span><span class="zz z3">z</span></span>';
const CAT_LG = '<span class="cat-loader big"><svg width="64" height="44" viewBox="0 0 32 22" fill="currentColor"><rect x="2" y="16.5" width="28" height="3.5" rx="1.75" opacity=".35"/><path d="M6.3 16.5c-1.9 0-3.3-1.3-3.3-3 0-.9.6-1.6 1.4-1.9C5.3 8 8.6 5.4 12.6 5.4c2.3 0 4.4.85 5.9 2.2l.5-2.3c.07-.33.5-.42.7-.15l1.6 2.1c.5-.1 1-.1 1.5-.02l1.7-2c.22-.26.64-.15.7.18l.4 2.35c1.3.9 2.1 2.3 2.1 3.9 0 1.4-.63 2.68-1.66 3.58.04.15.06.3.06.47 0 .95-.83 1.72-1.85 1.72H6.3z"/></svg><span class="zz z1">z</span><span class="zz z2">z</span><span class="zz z3">z</span></span>';

/* ---------- tabs ---------- */
document.querySelectorAll(".tab-btn").forEach(b => b.addEventListener("click", () => {
  document.querySelectorAll(".tab-btn").forEach(x => x.classList.toggle("active", x === b));
  document.querySelectorAll("section.view").forEach(v =>
    v.classList.toggle("active", v.id === "view-" + b.dataset.tab));
  history.replaceState(null, "", "#" + b.dataset.tab);
  if (b.dataset.tab === "reclaim") ensureReclaimLoaded();
}));
function gotoTab(name) { document.querySelector(`.tab-btn[data-tab=${name}]`).click(); }

/* ---------- library ---------- */
let ROWS = [];             // all smart-depth rows from /api/folders
let sortMode = "client";

function cmpClient(a, b) {
  const na = a.client === NO_CLIENT ? 1 : 0, nb = b.client === NO_CLIENT ? 1 : 0;
  if (na !== nb) return na - nb;
  const ca = a.client.toLowerCase(), cb = b.client.toLowerCase();
  if (ca !== cb) return ca < cb ? -1 : 1;
  const da = a.dsort ? [0, ...a.dsort] : [1, 0, 0, 0];
  const db = b.dsort ? [0, ...b.dsort] : [1, 0, 0, 0];
  for (let i = 0; i < 4; i++) if (da[i] !== db[i]) return da[i] - db[i];
  return b.size - a.size;
}

function renderLib() {
  const q = $("q").value.trim().toLowerCase();
  const fd = $("f-drive").value, ft = $("f-type").value;
  let rows = ROWS.filter(r =>
    (!fd || r.drive === fd) && (!ft || r.type === ft) &&
    (!q || r.client.toLowerCase().includes(q) || r.job.toLowerCase().includes(q)));
  rows = rows.slice().sort(sortMode === "size" ? (a, b) => b.size - a.size : cmpClient);
  const totalSize = rows.reduce((s, r) => s + r.size, 0);
  $("lib-stats").textContent =
    `${rows.length.toLocaleString()} รายการ · ${rows.reduce((s, r) => s + r.count, 0).toLocaleString()} ไฟล์ · ${human(totalSize)}`;
  const body = $("lib-body");
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="8" class="muted">ไม่มีรายการที่ตรงกับตัวกรอง</td></tr>`;
    return;
  }
  body.innerHTML = rows.map((r, i) => `
    <tr class="row" data-i="${i}" data-drive="${esc(r.drive)}" data-folder="${esc(r.folder)}">
      <td><span class="badge drive">${esc(r.drive)}</span></td>
      <td><span class="badge ${esc(r.type)}">${esc(r.type)}</span></td>
      <td class="clip" title="${esc(r.client)}">${r.client === NO_CLIENT ? '<span class="muted">—</span>' : esc(r.client)}</td>
      <td class="clip" title="${esc(r.job)}">${esc(r.job)}</td>
      <td>${r.dnorm ? esc(r.dnorm) + (r.cal ? ` <span class="badge cal">${esc(r.cal)}</span>` : "") : '<span class="muted">—</span>'}</td>
      <td class="num muted">${esc(r.myear)}</td>
      <td class="num">${r.count.toLocaleString()}</td>
      <td class="num">${esc(r.size_human)}</td>
    </tr>`).join("");
}

function human(n) {
  n = Number(n) || 0;
  for (const u of ["B", "KB", "MB", "GB", "TB"]) {
    if (Math.abs(n) < 1024) return u === "B" ? `${n.toFixed(0)}B` : `${n.toFixed(1)}${u}`;
    n /= 1024;
  }
  return `${n.toFixed(1)}PB`;
}

/* row click -> expand file list */
$("lib-body").addEventListener("click", async e => {
  const tr = e.target.closest("tr.row");
  if (!tr) return;
  const next = tr.nextElementSibling;
  if (next && next.classList.contains("detail")) { next.remove(); tr.classList.remove("open"); return; }
  document.querySelectorAll("tr.detail").forEach(x => x.remove());
  document.querySelectorAll("tr.row.open").forEach(x => x.classList.remove("open"));
  tr.classList.add("open");
  const det = document.createElement("tr");
  det.className = "detail";
  det.innerHTML = `<td colspan="8">${CAT_SM}กำลังโหลดไฟล์...</td>`;
  tr.after(det);
  const res = await api(`/api/files?drive=${encodeURIComponent(tr.dataset.drive)}&folder=${encodeURIComponent(tr.dataset.folder)}`);
  if (!res.ok) { det.innerHTML = `<td colspan="8">โหลดไม่สำเร็จ: ${esc(res.error)}</td>`; return; }
  const rows = res.rows.map(f => `
    <tr><td class="clip" style="max-width:560px" title="${esc(f.relpath)}">${esc(f.relpath)}</td>
    <td class="muted">${esc(f.ext)}</td><td class="num">${esc(f.size_human)}</td>
    <td class="num muted">${esc(f.mdate)}</td></tr>`).join("");
  det.innerHTML = `<td colspan="8"><div class="file-list">
    <div class="hint" style="margin-bottom:6px">${res.rows.length.toLocaleString()} ไฟล์${res.truncated ? " (แสดง 1,000 แรก)" : ""} ใน ${esc(tr.dataset.folder)}</div>
    <table>${rows}</table></div></td>`;
});

/* live filter + debounced server filename search */
let debounceT = null;
$("q").addEventListener("input", () => {
  renderLib();
  clearTimeout(debounceT);
  const kw = $("q").value.trim();
  if (kw.length < 2) { $("file-hits").innerHTML = ""; return; }
  debounceT = setTimeout(async () => {
    const res = await api(`/api/search?q=${encodeURIComponent(kw)}`);
    if (!res.ok || !res.rows.length) { $("file-hits").innerHTML = ""; return; }
    $("file-hits").innerHTML = `
      <div class="dedup-head">ไฟล์ที่ชื่อตรงกับ "${esc(kw)}" — ${res.rows.length.toLocaleString()}${res.truncated ? "+" : ""} ไฟล์</div>
      <div class="tbl-wrap"><table><tbody>
      ${res.rows.map(f => `<tr>
        <td><span class="badge drive">${esc(f.drive)}</span></td>
        <td class="clip" style="max-width:640px" title="${esc(f.relpath)}">${esc(f.relpath)}</td>
        <td class="num">${esc(f.size_human)}</td>
        <td class="num muted">${esc(f.mdate)}</td></tr>`).join("")}
      </tbody></table></div>`;
  }, 350);
});
["f-drive", "f-type"].forEach(id => $(id).addEventListener("change", renderLib));
$("sort-client").addEventListener("click", () => setSort("client"));
$("sort-size").addEventListener("click", () => setSort("size"));
function setSort(m) {
  sortMode = m;
  $("sort-client").classList.toggle("on", m === "client");
  $("sort-size").classList.toggle("on", m === "size");
  renderLib();
}

async function loadFolders() {
  const res = await api("/api/folders");
  if (!res.ok) {
    $("lib-body").innerHTML = `<tr><td colspan="8">โหลดไม่สำเร็จ: ${esc(res.error)}</td></tr>`;
    return;
  }
  ROWS = res.rows;
  const drives = [...new Set(ROWS.map(r => r.drive))].sort();
  $("f-drive").innerHTML = `<option value="">ทุกไดรฟ์</option>` +
    drives.map(d => `<option value="${esc(d)}">${esc(d)}</option>`).join("");
  renderLib();
}

/* ---------- drives ---------- */
/* ---------- home dashboard ---------- */
$("topbar-scan").addEventListener("click", () => gotoTab("scan"));
$("home-go-library").addEventListener("click", () => gotoTab("library"));
$("home-go-dedup").addEventListener("click", () => gotoTab("reclaim"));
$("home-go-scan").addEventListener("click", () => gotoTab("scan"));
$("home-first-scan").addEventListener("click", () => gotoTab("scan"));
$("home-first-cat").innerHTML = CAT_LG;

function relTime(ts) {
  if (!ts) return "—";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 90) return "เมื่อครู่";
  if (s < 3600) return `${Math.round(s / 60)} นาทีที่แล้ว`;
  if (s < 86400) return `${Math.round(s / 3600)} ชั่วโมงที่แล้ว`;
  if (s < 86400 * 30) return `${Math.round(s / 86400)} วันที่แล้ว`;
  return new Date(ts * 1000).toLocaleDateString("th-TH");
}

/* free-space class shared with the drives tab: <15% red, <30% amber */
function freeClass(pct) { return pct === null ? "" : pct < 15 ? "bad" : pct < 30 ? "warn" : "ok"; }

let homeStatsAnimated = false;
function renderHomeStats(drives) {
  const dash = $("home-dash"), first = $("home-first");
  if (!drives.length) { dash.hidden = true; first.hidden = false; return; }
  first.hidden = true; dash.hidden = false;

  const totalFiles = drives.reduce((s, d) => s + d.files, 0);
  const totalBytes = drives.reduce((s, d) => s + d.bytes, 0);
  const lastTs = drives.reduce((m, d) => Math.max(m, d.last_scanned || 0), 0);

  $("home-sub").textContent =
    `${drives.length} ไดรฟ์ · ${totalFiles.toLocaleString()} ไฟล์ · ${human(totalBytes)} — ค้นได้ทั้งหมดโดยไม่ต้องเสียบไดรฟ์`;
  $("kpi-last").textContent = relTime(lastTs);

  /* count-up once per session, then just set the numbers */
  const setNums = e => {
    $("kpi-drives").textContent = Math.round(drives.length * e).toLocaleString();
    $("kpi-files").textContent = Math.round(totalFiles * e).toLocaleString();
    $("kpi-size").textContent = human(totalBytes * e);
  };
  if (homeStatsAnimated) { setNums(1); }
  else {
    homeStatsAnimated = true;
    const dur = 900, start = performance.now(), easeOut = t => 1 - Math.pow(1 - t, 3);
    (function frame(now) {
      const e = easeOut(Math.min(1, (now - start) / dur));
      setNums(e);
      if (e < 1) requestAnimationFrame(frame);
    })(performance.now());
  }

  /* --- drive space: the ones that need attention first --- */
  const withSpace = drives.filter(d => d.total_bytes);
  const scored = withSpace.map(d => ({d, pct: d.free_bytes / d.total_bytes * 100}))
                          .sort((a, b) => a.pct - b.pct);
  const low = scored.filter(x => x.pct < 15).length;
  const pill = $("home-space-pill");
  if (low) { pill.hidden = false; pill.className = "pill alert"; pill.textContent = `${low} ลูกใกล้เต็ม`; }
  else { pill.hidden = false; pill.className = "pill"; pill.textContent = "ยังมีที่ว่างพอ"; }

  const space = $("home-space");
  if (!scored.length) {
    space.innerHTML = `<div class="home-note">ยังไม่มีข้อมูลพื้นที่ว่าง — ข้อมูลจะขึ้นหลังสแกนตอนเสียบไดรฟ์อยู่</div>`;
  } else {
    space.innerHTML = scored.slice(0, 6).map(({d, pct}) => {
      const cls = freeClass(pct);
      return `<div class="mini-row click" data-drive="${esc(d.label)}">
        <span class="mini-name" title="${esc(d.label)}">${esc(d.label)}</span>
        <span class="mini-bar ${cls}"><i style="width:${(100 - pct).toFixed(1)}%"></i></span>
        <span class="mini-meta ${cls === "ok" ? "" : cls}">${cls === "bad" ? "⚠ " : ""}ว่าง ${pct.toFixed(0)}%</span>
      </div>`;
    }).join("") +
    (scored.length > 6 ? `<div class="home-note">และอีก ${scored.length - 6} ไดรฟ์ — ดูทั้งหมดในแท็บ “ไดรฟ์”</div>` : "");
  }

  /* --- most recently scanned --- */
  const recent = drives.slice().sort((a, b) => (b.last_scanned || 0) - (a.last_scanned || 0)).slice(0, 6);
  $("home-recent").innerHTML = recent.map(d => `<div class="mini-row click" data-drive="${esc(d.label)}">
      <span class="mini-name" title="${esc(d.label)}">${esc(d.label)}</span>
      <span class="mini-meta">${d.files.toLocaleString()} ไฟล์ · ${esc(d.bytes_human)}</span>
      <span class="mini-meta">${relTime(d.last_scanned)}</span>
    </div>`).join("");

  /* jump straight into that drive's library view */
  document.querySelectorAll("#home-space .mini-row, #home-recent .mini-row").forEach(r =>
    r.addEventListener("click", () => {
      gotoTab("library");
      $("f-drive").value = r.dataset.drive;
      renderLib();
    }));
}

/* --- home file search (same endpoint as the library's filename search) --- */
let homeSearchT = null;
$("home-q").addEventListener("input", () => {
  clearTimeout(homeSearchT);
  const kw = $("home-q").value.trim();
  const box = $("home-hits");
  if (kw.length < 2) { box.innerHTML = ""; return; }
  homeSearchT = setTimeout(async () => {
    const res = await api(`/api/search?q=${encodeURIComponent(kw)}`);
    if (!res.ok) { box.innerHTML = `<div class="hint">ค้นหาไม่สำเร็จ: ${esc(res.error)}</div>`; return; }
    if (!res.rows.length) {
      box.innerHTML = `<div class="hint">ไม่พบไฟล์ที่ชื่อมีคำว่า “${esc(kw)}”</div>`;
      return;
    }
    box.innerHTML = `
      <div class="dedup-head">พบ ${res.rows.length.toLocaleString()}${res.truncated ? "+" : ""} ไฟล์ — คลิกเพื่อดูในคลังงาน</div>
      <div class="tbl-wrap"><table><tbody>
      ${res.rows.slice(0, 40).map(f => `<tr data-drive="${esc(f.drive)}">
        <td><span class="badge drive">${esc(f.drive)}</span></td>
        <td class="clip" style="max-width:620px" title="${esc(f.relpath)}">${esc(f.relpath)}</td>
        <td class="num">${esc(f.size_human)}</td>
        <td class="num muted">${esc(f.mdate)}</td></tr>`).join("")}
      </tbody></table></div>
      ${res.rows.length > 40 ? `<div class="home-note">แสดง 40 แถวแรก — ค้นต่อในแท็บ “คลังงาน” เพื่อดูทั้งหมด</div>` : ""}`;
    box.querySelectorAll("tr").forEach(tr => tr.addEventListener("click", () => {
      gotoTab("library");
      $("f-drive").value = tr.dataset.drive;
      $("q").value = kw;
      $("q").dispatchEvent(new Event("input"));
    }));
  }, 350);
});

async function loadDrives() {
  const res = await api("/api/drives");
  if (!res.ok) { $("drive-cards").innerHTML = esc(res.error); return; }
  ["rc-drive", "rc-type-drive"].forEach((id, i) => {
    const el = $(id);
    const first = el.options[0].outerHTML;
    el.innerHTML = first + res.drives.map(d =>
      `<option value="${esc(d.label)}">${esc(d.label)}</option>`).join("");
  });
  $("db-info").textContent =
    `${res.drives.length} ไดรฟ์ · ${res.drives.reduce((s, d) => s + d.files, 0).toLocaleString()} ไฟล์ใน catalog`;
  renderHomeStats(res.drives);
  if (!res.drives.length) {
    $("drive-cards").innerHTML = `<div class="hint">ยังไม่มีไดรฟ์ใน catalog — ไปที่แท็บ "สแกน" เพื่อเริ่ม</div>`;
    return;
  }
  $("drive-cards").innerHTML = res.drives.map(d => {
    const pct = d.total_bytes ? (d.free_bytes / d.total_bytes * 100) : null;
    const usedPct = pct === null ? 0 : 100 - pct;
    const cls = pct === null ? "" : pct < 15 ? "bad" : pct < 30 ? "warn" : "ok";
    const scanned = d.last_scanned ? new Date(d.last_scanned * 1000).toLocaleString("th-TH") : "-";
    return `<div class="card ${cls === "bad" ? "danger" : ""}" data-label="${esc(d.label)}" style="cursor:pointer">
      <h3>${esc(d.label)}</h3>
      ${pct === null ? `<div class="free-line muted">ไม่มีข้อมูลพื้นที่</div>` :
        `<div class="free-line ${cls}">${cls === "bad" ? "⚠ " : ""}ว่าง ${pct.toFixed(0)}% (${esc(d.free_human)})</div>
         <div class="bar"><div class="fill" style="width:${usedPct.toFixed(1)}%"></div></div>`}
      <div class="stats">${d.files.toLocaleString()} ไฟล์ใน catalog · ${esc(d.bytes_human)}<br>
        ${d.total_bytes ? `ความจุ ${esc(d.total_human)}` : ""}</div>
      <div class="scanned">สแกนล่าสุด: ${esc(scanned)}</div>
      <button class="card-del" data-label="${esc(d.label)}">ลบออกจาก catalog</button>
    </div>`;
  }).join("");
  document.querySelectorAll("#drive-cards .card").forEach(c => c.addEventListener("click", () => {
    gotoTab("library"); $("f-drive").value = c.dataset.label; renderLib();
  }));
  document.querySelectorAll("#drive-cards .card-del").forEach(btn => {
    btn.addEventListener("click", e => {
      e.stopPropagation();
      const label = btn.dataset.label;
      const wrap = document.createElement("div");
      wrap.className = "card-del-confirm";
      wrap.innerHTML = `<span>ลบ "${esc(label)}" ? ไฟล์จริงไม่ถูกแตะ</span>
        <button class="card-del-yes">ลบเลย</button>
        <button class="card-del-no">ยกเลิก</button>`;
      wrap.addEventListener("click", e2 => e2.stopPropagation());
      btn.replaceWith(wrap);
      wrap.querySelector(".card-del-yes").addEventListener("click", async () => {
        const res = await api("/api/forget", {method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({label})});
        if (!res.ok) {
          wrap.innerHTML = `<span class="card-del-err">${esc(res.error)}</span>`;
          return;
        }
        loadDrives();
        loadFolders();
      });
      wrap.querySelector(".card-del-no").addEventListener("click", () => loadDrives());
    });
  });
}

/* ---------- scan ---------- */
$("scan-btn").addEventListener("click", async () => {
  const path = $("scan-path").value.trim(), label = $("scan-label").value.trim();
  const box = $("scan-progress");
  box.className = "progress-box show";
  if (!path || !label) { box.classList.add("err"); box.textContent = "ต้องใส่ทั้ง path และ label"; return; }
  box.innerHTML = `${CAT_SM}กำลังเริ่ม...`;
  const res = await api("/api/scan", {method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({path, label})});
  if (!res.ok) { box.classList.add("err"); box.textContent = res.error; return; }
  $("scan-btn").disabled = true;
  pollJobs();
});

/* ---------- reclaim space ---------- */
let RC_SUMMARY = null, rcMode = "file", rcRows = [], rcOpenRule = null, rcJunkRows = {};

function rcTierBadge(t) {
  return t === "safe" ? '<span class="tier safe">ลบได้</span>'
                      : '<span class="tier review">ดูก่อน</span>';
}

/* one shared clipboard helper: WKWebView allows navigator.clipboard on
   127.0.0.1 (a secure context), execCommand is the fallback for old WebKit */
async function copyLines(lines, btn) {
  const text = lines.join("\n");
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); } finally { ta.remove(); }
  }
  if (btn) {
    const note = document.createElement("span");
    note.className = "rc-copied";
    note.textContent = `คัดลอก ${lines.length.toLocaleString()} บรรทัดแล้ว`;
    btn.parentNode.querySelectorAll(".rc-copied").forEach(n => n.remove());
    btn.parentNode.appendChild(note);
    setTimeout(() => note.remove(), 2600);
  }
}

function rcPathLines(rows) {
  return rows.map(r => `[${r.drive}] ${r.path || r.relpath}`);
}

function rcRowsTable(rows, withDup) {
  if (!rows.length) return `<div class="hint" style="padding:12px 0">ไม่มีรายการที่ตรงกับเงื่อนไข</div>`;
  return `<div class="tbl-wrap"><table><tbody>
    ${rows.map(r => `<tr>
      <td><span class="badge drive">${esc(r.drive)}</span></td>
      <td class="clip" style="max-width:560px" title="${esc(r.path || r.relpath)}">${esc(r.path || r.relpath)}</td>
      ${withDup ? `<td>${r.copies_elsewhere ? `<span class="rc-badge-dup">มีสำเนาอีก ${r.copies_elsewhere} ไดรฟ์</span>` : ""}</td>` : ""}
      <td class="num muted">${r.files > 1 ? r.files.toLocaleString() + " ไฟล์" : ""}</td>
      <td class="num">${esc(r.size_human)}</td>
      <td class="num muted">${r.mdate ? esc(r.mdate) : "?"}</td>
    </tr>`).join("")}
  </tbody></table></div>`;
}

async function loadReclaimSummary() {
  const res = await api("/api/reclaim/summary");
  if (!res.ok) { $("rc-junk").innerHTML = `<div class="hint" style="padding:16px">โหลดไม่สำเร็จ</div>`; return; }
  RC_SUMMARY = res;

  const junkTotal = res.junk.reduce((s, r) => s + r.bytes, 0);
  $("rc-kpi-junk").textContent = human(junkTotal);
  const top = res.bytype.total[0];
  $("rc-kpi-top").textContent = top ? `${top.label} · ${human(top.bytes)}` : "—";

  const shown = res.junk.filter(r => r.files > 0);
  $("rc-junk").innerHTML = shown.length ? shown.map(r => `
    <div class="rc-rule" data-rule="${esc(r.id)}">
      ${rcTierBadge(r.tier)}
      <div class="rc-rule-main">
        <div class="rc-rule-label">${esc(r.label)}</div>
        <div class="rc-rule-note">${esc(r.note)}</div>
      </div>
      <div class="rc-rule-files">${r.files.toLocaleString()} ไฟล์</div>
      <div class="rc-rule-size">${esc(r.bytes_human)}</div>
    </div>
    <div class="rc-detail" id="rc-detail-${esc(r.id)}" hidden></div>`).join("")
    : `<div class="hint" style="padding:16px">ไม่เจอ cache หรือไฟล์ขยะในคลังนี้</div>`;

  document.querySelectorAll("#rc-junk .rc-rule").forEach(row =>
    row.addEventListener("click", () => toggleJunkRule(row.dataset.rule)));

  if (res.bad_mtime && res.bad_mtime.files)
    $("rc-stale").innerHTML = `<br>มี ${res.bad_mtime.files.toLocaleString()} ไฟล์
      (${esc(res.bad_mtime.bytes_human)}) ที่วันที่แก้ไขเชื่อไม่ได้ — ไม่ถูกนับในรายการ “ไม่ได้แตะมานาน”`;

  renderByType();
}

async function toggleJunkRule(id) {
  const box = $("rc-detail-" + id);
  if (!box) return;
  if (!box.hidden) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = `<div class="hint">${CAT_SM} กำลังโหลด…</div>`;
  const res = await api(`/api/reclaim/junk?rule=${encodeURIComponent(id)}&limit=500`);
  if (!res.ok) { box.innerHTML = `<div class="hint">โหลดไม่สำเร็จ</div>`; return; }
  rcJunkRows[id] = res.rows;
  box.innerHTML = rcRowsTable(res.rows, false) + `
    <div class="rc-actions">
      <a class="btn btn-border" href="/api/reclaim/export?kind=junk&rule=${encodeURIComponent(id)}"
         download>ดาวน์โหลด checklist (.md)</a>
      <button class="btn btn-border" data-copy-rule="${esc(id)}">คัดลอก path ทั้งก้อน</button>
    </div>`;
  box.querySelector("[data-copy-rule]").addEventListener("click", e =>
    copyLines(rcPathLines(rcJunkRows[id] || []), e.target));
}

async function loadBigOld() {
  const box = $("rc-big");
  box.innerHTML = `<div class="hint">${CAT_SM} กำลังคำนวณ…</div>`;
  const gb = $("rc-gb").value, years = $("rc-years").value, drive = $("rc-drive").value;
  const res = await api(`/api/reclaim/bigold?gb=${gb}&years=${years}&mode=${rcMode}` +
                        `&drive=${encodeURIComponent(drive)}&limit=200`);
  if (!res.ok) { box.innerHTML = `<div class="hint">โหลดไม่สำเร็จ</div>`; return; }
  rcRows = res.rows;
  /* headline = every match, not just the rows on screen */
  const total = res.total || {items: rcRows.length,
                              bytes: rcRows.reduce((s, r) => s + r.size, 0)};
  $("rc-kpi-big").textContent = human(total.bytes);
  const capped = total.items > rcRows.length
    ? ` (แสดง ${rcRows.length.toLocaleString()} อันดับแรก)` : "";
  const qs = `kind=bigold&gb=${gb}&years=${years}&mode=${rcMode}&drive=${encodeURIComponent(drive)}&limit=1000`;
  box.innerHTML = `
    <div class="dedup-head">${total.items.toLocaleString()} รายการ${capped} ·
      คืนพื้นที่ได้ <span class="save">~${human(total.bytes)}</span></div>
    ${rcRowsTable(rcRows, rcMode === "file")}
    <div class="rc-actions">
      <a class="btn btn-border" href="/api/reclaim/export?${qs}" download>ดาวน์โหลด checklist (.md)</a>
      <button class="btn btn-border" id="rc-copy-big">คัดลอก path ทั้งก้อน</button>
    </div>`;
  $("rc-copy-big").addEventListener("click", e => copyLines(rcPathLines(rcRows), e.target));
}

function renderByType() {
  if (!RC_SUMMARY) return;
  const drive = $("rc-type-drive").value;
  const rows = drive ? (RC_SUMMARY.bytype.drives[drive] || []) : RC_SUMMARY.bytype.total;
  const max = rows.length ? rows[0].bytes : 1;
  $("rc-bytype").innerHTML = rows.length ? rows.map(r => `
    <div class="rc-bar">
      <span class="rc-bar-name">${esc(r.label)}</span>
      <span class="rc-bar-track"><i style="width:${(r.bytes / max * 100).toFixed(1)}%"></i></span>
      <span class="rc-bar-val">${esc(r.bytes_human)} · ${r.files.toLocaleString()} ไฟล์</span>
    </div>`).join("") : `<div class="hint" style="padding:12px 0">ไม่มีข้อมูล</div>`;
}

$("rc-mode-file").addEventListener("click", () => {
  rcMode = "file";
  $("rc-mode-file").classList.add("on"); $("rc-mode-folder").classList.remove("on");
  loadBigOld();
});
$("rc-mode-folder").addEventListener("click", () => {
  rcMode = "folder";
  $("rc-mode-folder").classList.add("on"); $("rc-mode-file").classList.remove("on");
  loadBigOld();
});
["rc-gb", "rc-years", "rc-drive"].forEach(id =>
  $(id).addEventListener("change", loadBigOld));
$("rc-type-drive").addEventListener("change", renderByType);

/* first visit to the tab does the work - not on every page load */
let reclaimLoaded = false;
function ensureReclaimLoaded() {
  if (reclaimLoaded) return;
  reclaimLoaded = true;
  loadReclaimSummary();
  loadBigOld();
}

/* ---------- dedup ---------- */

$("dedup-btn").addEventListener("click", async () => {
  const box = $("dedup-progress");
  box.className = "progress-box show";
  box.innerHTML = `${CAT_SM}กำลังหาไฟล์ซ้ำ (ครั้งแรกอาจใช้เวลาสักครู่ - กำลังสร้าง index)...`;
  $("dedup-results").innerHTML = "";
  const res = await api("/api/dedup", {method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({min_size: Number($("dedup-min").value)})});
  if (!res.ok) { box.classList.add("err"); box.textContent = res.error; return; }
  $("dedup-btn").disabled = true;
  pollJobs();
});

function renderDedup(j) {
  const box = $("dedup-progress");
  if (j.status === "done") {
    box.className = "progress-box";
    if (!j.groups.length) {
      $("dedup-results").innerHTML = `<div class="dedup-head">ไม่เจอไฟล์ซ้ำที่ขนาด ≥ ${human(j.min_size)}</div>`;
      return;
    }
    $("dedup-results").innerHTML = `
      <div class="dedup-head">เจอ ${j.group_count.toLocaleString()} กลุ่มไฟล์ซ้ำ ·
        คืนพื้นที่ได้ <span class="save">~${esc(j.total_waste_human)}</span>
        ${j.group_count > j.groups.length ? `(แสดง ${j.groups.length} กลุ่มแรก เรียงตามพื้นที่ที่คืนได้)` : ""}</div>
      ${j.groups.map(g => `<details class="dgroup"><summary>
          <span class="fname">${esc(g.filename)}</span>
          <span class="meta">${esc(g.size_human)} × ${g.copies} ชุด</span>
          <span class="waste">คืนได้ ${esc(g.waste_human)}</span></summary>
        <ul>${g.members.map(m => `<li><span class="badge drive">${esc(m.drive)}</span> ${esc(m.relpath)} <span class="muted">(${esc(m.mdate)})</span></li>`).join("")}</ul>
      </details>`).join("")}
      <div class="readonly-note">โหมดอ่านอย่างเดียว — เว็บนี้ไม่ลบ/ย้ายไฟล์ ใช้รายการนี้ไปตัดสินใจเองบนเครื่อง</div>`;
  } else if (j.status === "error") {
    box.className = "progress-box show err";
    box.textContent = "ผิดพลาด: " + j.error;
  }
}

/* ---------- job polling ---------- */
let pollT = null;
async function pollJobs() {
  clearTimeout(pollT);
  const res = await api("/api/jobs");
  if (!res.ok) return;
  const s = res.jobs.scan, d = res.jobs.dedup;
  const sbox = $("scan-progress");
  if (s.status === "running") {
    sbox.className = "progress-box show";
    sbox.innerHTML = `${CAT_LG}กำลังสแกน <b>${esc(s.label)}</b> ...
      <div class="big">${(s.count || 0).toLocaleString()} ไฟล์</div>`;
    $("scan-btn").disabled = true;
  } else if (s.status === "done" && $("scan-btn").disabled) {
    sbox.className = "progress-box show";
    sbox.innerHTML = `เสร็จแล้ว: <b>${esc(s.label)}</b> —
      <div class="big">${s.files.toLocaleString()} ไฟล์ · ${esc(s.bytes_human)}</div>
      ใช้เวลา ${Math.round(s.seconds)} วินาที
      ${s.disk_total ? `· พื้นที่ว่าง ${esc(s.disk_free_human)} / ${esc(s.disk_total_human)} (${s.pct_free}%)` : ""}`;
    $("scan-btn").disabled = false;
    loadDrives(); loadFolders();
  } else if (s.status === "error" && $("scan-btn").disabled) {
    sbox.className = "progress-box show err";
    sbox.textContent = "สแกนไม่สำเร็จ: " + s.error;
    $("scan-btn").disabled = false;
  }
  if (d.status === "running") {
    $("dedup-btn").disabled = true;
  } else if ($("dedup-btn").disabled && (d.status === "done" || d.status === "error")) {
    $("dedup-btn").disabled = false;
    renderDedup(d);
  } else if (d.status === "done" && $("dedup-results").innerHTML === "") {
    renderDedup(d);
  }
  if (s.status === "running" || d.status === "running") pollT = setTimeout(pollJobs, 1000);
}

/* ---------- drive plug-in watcher ---------- */
let knownVols = null;   // Map(path -> volume obj); null = no baseline yet
async function pollVolumes() {
  try {
    const v = await api("/api/volumes");
    if (v.ok) {
      const cur = new Map(v.volumes.map(x => [x.path, x]));
      $("volumes").innerHTML = v.volumes.map(x => `<option value="${esc(x.path)}">`).join("");
      if (knownVols !== null) {
        for (const [p, x] of cur) if (!knownVols.has(p)) showDriveToast(x);
        for (const p of knownVols.keys()) if (!cur.has(p)) removeDriveToast(p);
      }
      knownVols = cur;
    }
  } catch (e) { /* server briefly unreachable - keep polling */ }
  setTimeout(pollVolumes, 4000);
}

function showDriveToast(x) {
  if (document.querySelector(`.toast[data-path="${CSS.escape(x.path)}"]`)) return;
  const label = x.known_label || x.name;
  const sub = x.last_scanned
    ? `เคยสแกนเป็น "${x.known_label}" ล่าสุด ${new Date(x.last_scanned * 1000).toLocaleDateString("th-TH")}`
    : "ยังไม่เคยสแกนเข้า catalog";
  const el = document.createElement("div");
  el.className = "toast";
  el.dataset.path = x.path;
  el.innerHTML = `<div class="t-title">💾 เสียบไดรฟ์: ${esc(x.name)}</div>
    <div class="t-sub">${esc(sub)}</div>
    <div class="t-actions">
      <button class="btn t-scan">สแกนเลย</button>
      <button class="btn btn-border t-close">ปิด</button>
    </div>`;
  el.querySelector(".t-scan").addEventListener("click", async () => {
    gotoTab("scan");
    $("scan-path").value = x.path;
    $("scan-label").value = label;
    el.remove();
    const box = $("scan-progress");
    box.className = "progress-box show";
    box.innerHTML = `${CAT_SM}กำลังเริ่ม...`;
    const res = await api("/api/scan", {method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({path: x.path, label})});
    if (!res.ok) { box.classList.add("err"); box.textContent = res.error; return; }
    $("scan-btn").disabled = true;
    pollJobs();
  });
  el.querySelector(".t-close").addEventListener("click", () => el.remove());
  $("drive-toasts").appendChild(el);
}

function removeDriveToast(p) {
  const el = document.querySelector(`.toast[data-path="${CSS.escape(p)}"]`);
  if (el) el.remove();
}

/* ---------- update check + self-update ---------- */
async function checkUpdate() {
  try {
    const res = await api("/api/update");
    if (!res.ok) return;
    if (res.available && res.enabled && sessionStorage.getItem("hddcat-update-dismissed") !== "1") {
      $("update-banner").hidden = false;
      $("ub-text").innerHTML = `🐈 มีเวอร์ชันใหม่ v${esc(res.latest)} — ${esc(res.notes || "")}`;
    }
  } catch (e) { /* offline - fail silently, try again next launch */ }
}

$("ub-dismiss").addEventListener("click", () => {
  $("update-banner").hidden = true;
  sessionStorage.setItem("hddcat-update-dismissed", "1");
});

$("ub-skip").addEventListener("click", async (e) => {
  e.preventDefault();
  await api("/api/update", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({action: "disable"})});
  $("update-banner").hidden = true;
});

$("ub-apply").addEventListener("click", async () => {
  $("ub-apply").disabled = true;
  $("ub-text").innerHTML = `${CAT_SM}กำลังดาวน์โหลดและติดตั้งอัปเดต...`;
  const res = await api("/api/update", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({action: "apply"})});
  if (!res.ok) {
    $("ub-text").textContent = "ผิดพลาด: " + res.error;
    $("ub-apply").disabled = false;
    return;
  }
  pollUpdateJob();
});

let updatePollT = null;
async function pollUpdateJob() {
  clearTimeout(updatePollT);
  const res = await api("/api/jobs");
  if (!res.ok) return;
  const u = res.jobs.update || {};
  if (u.status === "running") {
    updatePollT = setTimeout(pollUpdateJob, 1500);
  } else if (u.status === "done") {
    $("update-banner").hidden = false;
    $("ub-text").innerHTML =
      `✅ อัปเดตเป็น v${esc(u.version)} แล้ว — ปิดแอป (คลิกขวาไอคอนแมวใน Dock &gt; Quit) แล้วเปิดใหม่`;
    $("ub-actions").style.display = "none";
  } else if (u.status === "error") {
    $("ub-text").textContent = "อัปเดตไม่สำเร็จ: " + u.error;
    $("ub-apply").disabled = false;
  }
}

/* ---------- version chip (manual check) ---------- */
const VC_LABEL = `HDDCAT v${HDDCAT_VERSION} · เช็คอัปเดต`;
function vcReset() {
  const chip = $("version-chip");
  chip.classList.remove("vc-available");
  chip.innerHTML = VC_LABEL;
}
$("version-chip").addEventListener("click", async () => {
  const chip = $("version-chip");
  if (chip.disabled) return;
  chip.disabled = true;
  chip.classList.remove("vc-available");
  chip.innerHTML = `${CAT_SM}กำลังเช็ค...`;
  try {
    const res = await api("/api/update?force=1");
    if (!res.ok) throw new Error("check failed");
    if (res.available) {
      chip.classList.add("vc-available");
      chip.innerHTML = `มีเวอร์ชันใหม่ v${esc(res.latest)} ↑`;
      $("update-banner").hidden = false;
      $("ub-text").innerHTML = `🐈 มีเวอร์ชันใหม่ v${esc(res.latest)} — ${esc(res.notes || "")}`;
      sessionStorage.removeItem("hddcat-update-dismissed");
    } else {
      chip.innerHTML = `✓ ล่าสุดแล้ว (v${esc(res.current)})`;
      setTimeout(vcReset, 2500);
    }
  } catch (e) {
    chip.innerHTML = "เช็คไม่ได้ (ออฟไลน์?)";
    setTimeout(vcReset, 2500);
  } finally {
    chip.disabled = false;
  }
});

/* ---------- init ---------- */
(async () => {
  loadDrives();
  loadFolders();
  pollJobs();
  pollVolumes();
  checkUpdate();
  const h = location.hash.slice(1);
  if (h && document.querySelector('.tab-btn[data-tab="' + h + '"]')) gotoTab(h);
  if (location.hash === "#demo-toast") {
    gotoTab("scan");
    /* demo hook: deterministic toast for docs/screenshots */
    showDriveToast({ path: "/Volumes/WD-4TB-01", name: "WD-4TB-01", known_label: "WD-4TB-01", last_scanned: 1784130000 });
  }
})();
</script>
</body>
</html>
"""


def cmd_serve(args):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse, parse_qs

    db_path = args.db
    assets_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
    dist_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):
            pass  # keep the terminal quiet

        def _send(self, code, body, ctype):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False),
                       "application/json; charset=utf-8")

        def do_GET(self):
            try:
                u = urlparse(self.path)
                q = parse_qs(u.query)
                route = u.path
                if route == "/":
                    html = INDEX_HTML.replace("{{VERSION}}", __version__)
                    self._send(200, html, "text/html; charset=utf-8")
                elif route == "/theme.css":
                    # optional override file next to catalog.py (Touch's custom CSS)
                    theme = os.path.join(os.path.dirname(os.path.abspath(__file__)), "theme.css")
                    css = "/* no theme.css - using built-in tokens */"
                    if os.path.exists(theme):
                        with open(theme, encoding="utf-8") as f:
                            css = f.read()
                    self._send(200, css, "text/css; charset=utf-8")
                elif route == "/api/folders":
                    conn = get_conn(db_path)
                    rows = conn.execute(
                        "SELECT drive_label, relpath, size, mtime FROM files").fetchall()
                    conn.close()
                    out = sort_folders(build_smart_folders(rows),
                                       q.get("sort", ["client"])[0])
                    for d in out:
                        d["size_human"] = human_size(d["size"])
                    self._json({"ok": True, "rows": out})
                elif route == "/api/drives":
                    conn = get_conn(db_path)
                    drives = drives_overview(conn)
                    conn.close()
                    for d in drives:
                        d["bytes_human"] = human_size(d["bytes"])
                        d["total_human"] = human_size(d["total_bytes"]) if d["total_bytes"] else None
                        d["free_human"] = human_size(d["free_bytes"]) if d["free_bytes"] is not None else None
                    self._json({"ok": True, "drives": drives})
                elif route == "/api/search":
                    kw = q.get("q", [""])[0].strip()
                    if len(kw) < 2:
                        self._json({"ok": True, "rows": [], "truncated": False})
                        return
                    conn = get_conn(db_path)
                    rows = search_files(conn, kw, limit=501)
                    conn.close()
                    truncated = len(rows) > 500
                    self._json({"ok": True, "truncated": truncated, "rows": [
                        {"drive": r[0], "relpath": r[1], "size": r[2],
                         "size_human": human_size(r[2]),
                         "mdate": time.strftime("%Y-%m-%d", time.localtime(r[3]))}
                        for r in rows[:500]]})
                elif route == "/api/reclaim/summary":
                    conn = get_conn(db_path)
                    payload = {"ok": True,
                               "junk": reclaim_junk_summary(conn),
                               "bytype": reclaim_by_type(conn),
                               "bad_mtime": reclaim_bad_mtime(conn)}
                    conn.close()
                    self._json(payload)
                elif route == "/api/reclaim/junk":
                    conn = get_conn(db_path)
                    rows = reclaim_junk_files(conn, q.get("rule", [""])[0],
                                              limit=int(q.get("limit", ["500"])[0]))
                    conn.close()
                    if rows is None:
                        self._json({"ok": False, "error": "ไม่รู้จักกฎนี้"}, 400)
                    else:
                        self._json({"ok": True, "rows": rows})
                elif route == "/api/reclaim/bigold":
                    conn = get_conn(db_path)
                    min_bytes = int(float(q.get("gb", ["1"])[0]) * 1024 ** 3)
                    years = float(q.get("years", ["3"])[0])
                    drive = q.get("drive", [""])[0] or None
                    mode = q.get("mode", ["file"])[0]
                    rows = reclaim_mark_duplicates(conn, reclaim_bigold(
                        conn, min_bytes, years, drive=drive, mode=mode,
                        limit=int(q.get("limit", ["200"])[0])))
                    total = reclaim_bigold_total(conn, min_bytes, years, drive=drive, mode=mode)
                    conn.close()
                    self._json({"ok": True, "rows": rows, "total": total})
                elif route == "/api/reclaim/export":
                    conn = get_conn(db_path)
                    if q.get("kind", ["bigold"])[0] == "junk":
                        rule_id = q.get("rule", [""])[0]
                        rule = _RULES_BY_ID.get(rule_id)
                        rows = reclaim_junk_files(conn, rule_id, limit=5000) or []
                        title = "ของที่ลบได้ — " + (rule["label"] if rule else "junk")
                        note = rule["note"] if rule else ""
                    else:
                        rows = reclaim_mark_duplicates(conn, reclaim_bigold(
                            conn,
                            int(float(q.get("gb", ["1"])[0]) * 1024 ** 3),
                            float(q.get("years", ["3"])[0]),
                            drive=q.get("drive", [""])[0] or None,
                            mode=q.get("mode", ["file"])[0],
                            limit=int(q.get("limit", ["1000"])[0])))
                        title = "ใหญ่และไม่ได้แตะมานาน"
                        note = ("ตรวจก่อนลบทุกครั้ง - รายการนี้ดูจากข้อมูลใน catalog "
                                "ไม่ได้เปิดอ่านไฟล์จริง")
                    conn.close()
                    data = reclaim_markdown(title, rows, note).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/markdown; charset=utf-8")
                    self.send_header("Content-Disposition",
                                     'attachment; filename="hddcat-reclaim.md"')
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif route == "/api/files":
                    drive = q.get("drive", [""])[0]
                    folder = q.get("folder", [""])[0]
                    conn = get_conn(db_path)
                    rows = files_in_folder(conn, drive, folder, limit=1000)
                    conn.close()
                    self._json({"ok": True, "rows": rows, "truncated": len(rows) >= 1000})
                elif route == "/api/jobs":
                    self._json({"ok": True, "jobs": _jobs_snapshot()})
                elif route == "/api/update":
                    force = q.get("force", ["0"])[0] in ("1", "true", "yes")
                    st = check_update(force=force)
                    latest = st.get("latest")
                    available = bool(latest) and _is_newer(latest, __version__)
                    self._json({"ok": True, "current": __version__, "latest": latest,
                                "url": st.get("url"), "notes": st.get("notes") or "",
                                "available": available, "enabled": st.get("enabled", True)})
                elif route == "/api/volumes":
                    vols = []
                    try:
                        conn = get_conn(db_path)
                        known = {r[0]: r[1] for r in conn.execute(
                            "SELECT drive_label, last_scanned FROM drives")}
                        conn.close()
                        for v in sorted(os.listdir("/Volumes")):
                            if v.startswith("."):
                                continue
                            full = os.path.join("/Volumes", v)
                            if os.path.islink(full):
                                continue  # e.g. "Macintosh HD" firmlink to /
                            vols.append({"path": full, "name": v,
                                         "known_label": v if v in known else None,
                                         "last_scanned": known.get(v)})
                    except OSError:
                        pass
                    self._json({"ok": True, "volumes": vols})
                elif route.startswith("/assets/"):
                    name = os.path.basename(route[len("/assets/"):])
                    ctypes = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                              ".png": "image/png", ".webp": "image/webp",
                              ".svg": "image/svg+xml"}
                    ext = os.path.splitext(name)[1].lower()
                    fpath = os.path.join(assets_dir, name)
                    if ext in ctypes and os.path.isfile(fpath):
                        with open(fpath, "rb") as f:
                            self._send(200, f.read(), ctypes[ext])
                    else:
                        self._json({"ok": False, "error": "not found"}, 404)
                elif route == "/download/HDDCAT.zip":
                    zpath = os.path.join(dist_dir, "HDDCAT.zip")
                    if os.path.isfile(zpath):
                        with open(zpath, "rb") as f:
                            data = f.read()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/zip")
                        self.send_header("Content-Disposition",
                                          'attachment; filename="HDDCAT.zip"')
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                    else:
                        self._json({"ok": False, "error": "not found"}, 404)
                else:
                    self._json({"ok": False, "error": "not found"}, 404)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                try:
                    self._json({"ok": False, "error": str(e)}, 500)
                except OSError:
                    pass

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/api/scan":
                    path = (body.get("path") or "").strip()
                    label = (body.get("label") or "").strip()
                    if not path or not label:
                        self._json({"ok": False, "error": "ต้องใส่ทั้ง path และ label"}, 400)
                        return
                    if not os.path.isdir(path):
                        self._json({"ok": False, "error": f"ไม่พบ path {path}"}, 400)
                        return
                    ok, err = _start_scan_job(db_path, path, label)
                    self._json({"ok": ok, "error": err}, 200 if ok else 409)
                elif self.path == "/api/dedup":
                    try:
                        min_size = max(0, int(body.get("min_size") or 1048576))
                    except (TypeError, ValueError):
                        min_size = 1048576
                    ok, err = _start_dedup_job(db_path, min_size)
                    self._json({"ok": ok, "error": err}, 200 if ok else 409)
                elif self.path == "/api/update":
                    action = (body.get("action") or "").strip()
                    if action == "disable":
                        s = _load_update_settings()
                        s["enabled"] = False
                        _save_update_settings(s)
                        self._json({"ok": True, "enabled": False})
                    elif action == "enable":
                        s = _load_update_settings()
                        s["enabled"] = True
                        _save_update_settings(s)
                        self._json({"ok": True, "enabled": True})
                    elif action == "apply":
                        ok, err = _start_update_job()
                        self._json({"ok": ok, "error": err}, 200 if ok else 409)
                    else:
                        self._json({"ok": False, "error": "unknown action"}, 400)
                elif self.path == "/api/forget":
                    label = (body.get("label") or "").strip()
                    if not label:
                        self._json({"ok": False, "error": "ต้องระบุ label"}, 400)
                        return
                    with _JOBS_LOCK:
                        scanning = _JOBS["scan"].get("status") == "running"
                    if scanning:
                        self._json({"ok": False, "error": "รอ scan เสร็จก่อนค่อยลบ"}, 409)
                        return
                    conn = get_conn(db_path)
                    n = forget_drive(conn, label)
                    conn.close()
                    if n is None:
                        self._json({"ok": False, "error": "ไม่พบ drive นี้ใน catalog"}, 404)
                    else:
                        self._json({"ok": True, "files_removed": n})
                else:
                    self._json({"ok": False, "error": "not found"}, 404)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                try:
                    self._json({"ok": False, "error": str(e)}, 500)
                except OSError:
                    pass

    httpd = None
    port = args.port
    for cand in range(args.port, args.port + 20):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", cand), Handler)
            port = cand
            break
        except OSError:
            continue
    if httpd is None:
        print(f"ERROR: หา port ว่างไม่ได้ (ลอง {args.port}-{args.port + 19})")
        sys.exit(1)
    url = f"http://127.0.0.1:{port}/"
    print(f"HDD Catalog web UI: {url}")
    print("เปิดเฉพาะในเครื่องนี้ (127.0.0.1) - กด Ctrl+C เพื่อปิด")
    if getattr(args, "exit_with_parent", False):
        # HDDCAT.app runs this as a child process. If the app is force-quit or
        # crashes it never gets to terminate us, and the orphan keeps holding
        # the port and the database; getppid() flipping to 1 (or anything else)
        # means our parent is gone.
        original_ppid = os.getppid()

        def _watch_parent():
            while True:
                time.sleep(2)
                if os.getppid() != original_ppid:
                    os._exit(0)

        threading.Thread(target=_watch_parent, daemon=True).start()
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nปิด server แล้ว")


def main():
    p = argparse.ArgumentParser(description="HDD Catalog & Consolidation Tool")
    p.add_argument("--db", default=DB_DEFAULT)
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("scan", help="scan a drive and catalog its files")
    sp.add_argument("drive_path")
    sp.add_argument("--label", required=True, help="unique label for this drive e.g. WD-4TB-01")
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("search", help="search cataloged files by name")
    sp.add_argument("keyword")
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("report", help="summary per drive")
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("groups", help="suggest consolidation groups across drives")
    sp.add_argument("--threshold", type=float, default=0.72)
    sp.add_argument("--min-drives", type=int, default=2)
    sp.add_argument("--by-client", action="store_true",
                     help="group by client/company instead of raw folder name - "
                          "strips dates/months first so 'FILLSTIM 25Feb' and "
                          "'2026-03-10 FILLSTIM at Sheraton' cluster together, "
                          "even on the same drive")
    sp.add_argument("--min-jobs", type=int, default=2,
                     help="(--by-client only) minimum number of matching folders to report")
    sp.add_argument("--max-token-spread", type=int, default=6,
                     help="(--by-client only) ignore a word as a matching key if it appears "
                          "in more than this many folders (filters out generic words)")
    sp.set_defaults(func=cmd_groups)

    sp = sub.add_parser("dedup", help="หาไฟล์ซ้ำจาก catalog (เทียบชื่อไฟล์+ขนาด, อ่านอย่างเดียว)")
    sp.add_argument("--min-size-mb", type=float, default=1.0,
                     help="ขนาดไฟล์ขั้นต่ำที่สนใจ (MB, default 1)")
    sp.add_argument("--limit", type=int, default=100,
                     help="จำนวนกลุ่มสูงสุดที่แสดง (default 100)")
    sp.set_defaults(func=cmd_dedup)

    sp = sub.add_parser("reclaim",
                         help="หาของที่ลบ/ย้ายได้เพื่อคืนพื้นที่ (อ่านอย่างเดียว ไม่ลบให้)")
    sp.add_argument("--junk", action="store_true", help="สรุป cache/ไฟล์ขยะที่ลบได้ (ค่าเริ่มต้น)")
    sp.add_argument("--big-old", action="store_true", help="ไฟล์ใหญ่ที่ไม่ถูกแตะมานาน")
    sp.add_argument("--by-type", action="store_true", help="พื้นที่หายไปกับไฟล์ชนิดไหน")
    sp.add_argument("--folders", action="store_true",
                     help="(--big-old) สรุปเป็นโฟลเดอร์งานแทนไฟล์เดี่ยว")
    sp.add_argument("--min-gb", type=float, default=1.0, help="(--big-old) ขนาดขั้นต่ำ GB")
    sp.add_argument("--years", type=float, default=3.0, help="(--big-old) ไม่ถูกแตะกี่ปี")
    sp.add_argument("--limit", type=int, default=100)
    sp.add_argument("--md", help="(--big-old) เขียน checklist Markdown ออกไฟล์")
    sp.set_defaults(func=cmd_reclaim)

    sp = sub.add_parser("forget", help="ลบ drive ออกจาก catalog (ลบแค่ข้อมูลใน DB ไม่แตะไฟล์จริง)")
    sp.add_argument("label")
    sp.add_argument("--yes", action="store_true", help="ยืนยันการลบ")
    sp.set_defaults(func=cmd_forget)

    sp = sub.add_parser("build-dist", help="สร้าง dist/HDDCAT.zip สำหรับแจกจ่าย")
    sp.set_defaults(func=cmd_build_dist)

    sp = sub.add_parser("serve", help="เปิด local web UI (127.0.0.1 เท่านั้น)")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--no-browser", action="store_true",
                     help="ไม่ต้องเปิด browser อัตโนมัติ")
    sp.add_argument("--exit-with-parent", action="store_true",
                     help="ปิดตัวเองเมื่อโปรเซสแม่หายไป (HDDCAT.app ใช้ตอนเรียก server)")
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("export-folders-csv",
                         help="export folder-level rollup (grouped by top folder) - much shorter than full file list")
    sp.add_argument("output")
    sp.add_argument("--depth", type=int, default=1,
                     help="how many path levels deep to group by (default 1 = top-level folder)")
    sp.add_argument("--smart-depth", action="store_true",
                     help="auto-skip a leading year-only folder (e.g. '2025/JobA' -> 'JobA') "
                          "so flat drives and year-wrapped drives both show job folders correctly")
    sp.add_argument("--sort", choices=["client", "size"], default="client",
                     help="(smart-depth only) client=group by client then date (default), "
                          "size=largest first")
    sp.set_defaults(func=cmd_export_folders_csv)

    sp = sub.add_parser("export-csv", help="export full catalog as a CSV file")
    sp.add_argument("output", help="output path e.g. catalog.csv")
    sp.set_defaults(func=cmd_export_csv)

    sp = sub.add_parser("export-obsidian", help="export catalog as Obsidian notes")
    sp.add_argument("vault_folder")
    sp.set_defaults(func=cmd_export_obsidian)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

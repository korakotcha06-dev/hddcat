# Contributing to HDDCAT

Thanks for wanting to help! HDDCAT is intentionally a strange animal — please read the ground rules before opening a PR.

## The four rules (load-bearing, non-negotiable)

1. **Python stdlib only.** No pip dependencies. If it can't `import` on a fresh macOS python3, it doesn't go in.
2. **One file.** Everything — engine, CLI, HTTP server, UI — lives in `catalog.py`. Users double-click and it works. The single exception is `shell/HDDCATShell.swift`, the native macOS window wrapper: it may start the server and display it, and nothing else. Any logic that belongs to the program itself goes in `catalog.py`, and `catalog.py` must stay fully usable with no shell at all.
3. **Vanilla frontend.** The UI is plain HTML/CSS/JS inside `INDEX_HTML`. No frameworks, no build step, no CDN required for core function.
4. **Local-first.** User data never leaves the machine. The only network call is the optional daily version check (sends the version number, nothing else). Anything beyond that must be opt-in and loudly disclosed.

PRs that break these rules will be declined even if they're good code — fork freely instead, that's what MIT is for.

## Dev setup

```
git clone https://github.com/korakotcha06-dev/hddcat.git
cd hddcat
python3 catalog.py --db scratch.db serve --port 8788
```

There is nothing to install. Use a scratch `--db`; never commit a .db.

Working on the native window? Build it with `./shell/build-shell.sh` (needs Xcode Command Line Tools), then run the binary directly with dev overrides instead of assembling a bundle:

```
HDDCAT_CATALOG=$PWD/catalog.py HDDCAT_DB=$PWD/scratch.db \
HDDCAT_PORT=8788 HDDCAT_DEBUG=1 ./shell/build/HDDCAT
```

`HDDCAT_DEBUG=1` also enables the Web Inspector (right-click > Inspect Element).

## Before you open a PR

- `python3 -m py_compile catalog.py`
- Existing CLI commands still work (`report`, `search`, `export-folders-csv --smart-depth` output should be unchanged unless that's the point of the PR)
- `python3 catalog.py build-dist` still produces a working zip (unzip it, open the .app or run the .command path, scan a small folder)
- Keep diffs small and focused; Thai or English both welcome in issues/PRs

## Good first targets

- Windows/Linux: volume detection (`/Volumes` is macOS-specific), path handling, `.app` equivalent
- Opt-in content hashing for dedup verification on mounted drives
- EN locale for the UI strings
- Performance: the library view aggregates every row per load — a cached rollup table would help >5M-file catalogs

## Releases (maintainer)

Bump `__version__` → `python3 catalog.py build-dist` → point the download buttons at `HDDCAT-<ver>.zip` and deploy the site → upload the zip under both that name and `HDDCAT.zip` → `gh release create vX.Y.Z dist/HDDCAT.zip --target <full SHA>` → bump `site/version-current.json`.

**Never bump `site/version.json`.** It is frozen at 1.1.2 on purpose: the in-app updater in releases up to 1.1.2 unzips without restoring the executable bit, so an update it performs leaves the app unopenable, and that cannot be fixed from the server. nginx picks the feed by `User-Agent: HDDCAT/<version>` — 1.1.2 and older (and anything unidentified) get the frozen `version.json`, 1.2.0 and newer get `version-current.json`. The map lives in `conf.d/hddcat-update-feed.conf` on the web host.

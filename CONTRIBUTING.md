# Contributing to HDDCAT

Thanks for wanting to help! HDDCAT is intentionally a strange animal — please read the ground rules before opening a PR.

## The four rules (load-bearing, non-negotiable)

1. **Python stdlib only.** No pip dependencies. If it can't `import` on a fresh macOS python3, it doesn't go in.
2. **One file.** Everything — engine, CLI, HTTP server, web UI — lives in `catalog.py`. Users double-click and it works.
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

Bump `__version__` → `python3 catalog.py build-dist` → update `site/version.json` + deploy → `gh release create vX.Y.Z dist/HDDCAT.zip`

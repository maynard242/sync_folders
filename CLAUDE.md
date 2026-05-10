# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`sync.py` — a stdlib-only Python rsync wrapper. `compare.py` — a stdlib-only read-only directory comparator built on `rsync --dry-run --itemize-changes`. `gui.py` — a stdlib-only tkinter front-end that subprocesses `sync.py`. `tests/test_naming.py` — end-to-end naming-robustness tests.

Three sync modes:
- `mirror` — source → dest with `--delete` (dest becomes exact copy)
- `additive` — source → dest, never delete on dest
- `two-way` — bidirectional, newer file wins (`rsync --update` run in both directions; local↔remote OK, remote↔remote rejected)

Endpoints can be local paths, `[user@]host:/path` (SSH), or `rsync://...` (rsync daemon). Detected by regex in `parse_endpoint()`.

Dry-run is the default; `--execute` is required to actually transfer.

## Run

```bash
./sync.py SRC DST --mode {mirror,additive,two-way}        # dry-run
./sync.py SRC DST --mode mirror --execute                 # transfer
./sync.py user@host:/data ./local --mode additive --ssh-port 2222 --ssh-key ~/.ssh/id_ed25519
./sync.py ./Photos user@nas:/volume1/photos --mode mirror --execute \
        --no-perms --nas-excludes --modify-window 1       # NAS-safe sync
./compare.py SRC DST [--checksum] [--nas-excludes]        # read-only diff
./gui.py                                                  # launch GUI
python3 tests/test_naming.py                              # run tests
```

Default log: `~/Library/Logs/sync_folders/sync.log` on macOS, `$XDG_STATE_HOME/sync_folders/sync.log` (or `~/.local/state/...`) on Linux. See `default_log_file()`.

## Architecture notes

- `build_rsync()` is the only place that knows about modes. To add a mode, return the right list of argv lists from there — everything else is mode-agnostic.
- `two-way` returns **two** rsync invocations. `--update` (skip-newer) is the conflict policy; there's no state file, so simultaneous edits on both sides will lose the older one silently. Don't promise stronger guarantees without adding state tracking (or swapping in unison).
- Network: when either endpoint is remote, `--compress` is auto-added and `-e ssh ...` is built from `--ssh-port`/`--ssh-key`/`--ssh-opts`. Local-only syncs skip both unless `--compress` is forced.
- Output decoding: `subprocess.Popen` uses `errors="replace"`. Apple's stock rsync (2.6.9) emits non-UTF-8 bytes for filenames with emoji/CJK; without this the wrapper crashes mid-sync.
- rsync version is checked at startup; <3.0.0 emits a warning (Apple ships 2.6.9; suggest `brew install rsync`).
- Trailing slashes on src/dst matter to rsync — added in `Endpoint.rsync_arg()` so callers pass plain paths.
- NAS options (`--no-perms`, `--nas-excludes`, `--modify-window`) are off by default. `--no-perms` swaps `-a` for `-rlt` so cross-filesystem syncs (macOS APFS ↔ Synology ext4 ↔ FAT/exFAT) don't churn on permission/owner bits the destination can't represent.

## compare.py

Read-only diff. Runs `rsync -ni --delete` and parses the itemized output: `+++++++++` flags = src-only (new), `*deleting` = dst-only, anything else with a transfer flag (`>`/`<`/`c`) = modified. Imports `parse_endpoint`, `build_ssh_e`, and `NAS_EXCLUDES` from `sync.py` to keep endpoint detection identical. Same-host remote↔remote pairs (e.g. `user@nas:/a` vs `user@nas:/b`) are detected and the rsync runs over a single ssh hop on the remote host — avoids the rsync limitation that bans remote↔remote in normal mode. ANSI color is auto-disabled on non-TTYs and when `NO_COLOR` is set.

## GUI architecture

`gui.py` is a thin shell. It builds a tkinter form, shells out to `sync.py` via `subprocess.Popen` with `start_new_session=True`, runs a daemon reader thread that pushes lines onto a `queue.Queue`, and a Tk `after()` poller drains the queue into a `ScrolledText`. Cancel sends `SIGTERM` to the process group (so child rsync dies too). No shared state between threads except the queue.

Don't import sync.py functions directly — the subprocess boundary is what makes the run cancellable and keeps a misbehaving rsync from killing the GUI.

## Tests

`tests/test_naming.py` builds a torture tree (spaces, mixed case, apostrophes, brackets, `& $ # @`, Latin accents, CJK, emoji, hidden files, deep nesting, paths-with-spaces, temp-dir-with-spaces) and runs all three modes end-to-end with content verification. Filenames are compared in NFC because APFS stores NFD and ext4 stores whatever you wrote — without normalization the same name fails equality across filesystems.

If you add features that change rsync invocation, extend `tests/test_naming.py` rather than writing ad-hoc smoke tests.

## Conventions

- Stdlib only. tkinter ships with python on macOS and Linux; don't pull in PyQt/wx/etc.
- Logging goes to **both** the log file and stdout — the file is the audit trail, stdout is what the GUI streams.
- Dry-run default is load-bearing: never flip it. If you add destructive features (e.g. `--delete-excluded`), keep them gated behind `--execute`.
- Python in 3.10+ syntax (`X | None`, structural pattern matching OK if it helps).

## Known limitations

- Two-way conflict resolution is mtime-based with second resolution; near-simultaneous edits on both sides can drop one.
- Remote↔remote sync is rejected (rsync limitation, not ours).
- Local paths containing a colon before the first `/` will be misclassified as SSH endpoints (`notes:scratch` looks like `host:path`). Workaround: use absolute paths or `./notes:scratch`.
- Apple's stock rsync (2.6.9, 2006) lacks newer flags. Most things work; if you need `--info=progress2` etc., install newer rsync via Homebrew.

# sync_folders

A small, stdlib-only Python wrapper around `rsync` — plus a tkinter GUI front-end and a read-only directory comparator. Syncs folder pairs locally or over the network, with a paranoid dry-run default and an end-to-end naming-robustness test suite.

## Why

`rsync` is the right tool. Its flag surface is large, easy to misuse, and easy to forget between machines. This wrapper picks safe defaults (dry-run by default, `errors="replace"` on subprocess output so emoji filenames don't crash the run) and exposes the common knobs through a CLI and a GUI.

## Install

No dependencies beyond Python 3.10+ and `rsync` on `PATH`. Tk ships with python on macOS and most Linux distros.

```bash
git clone https://github.com/maynard242/sync_folders.git
cd sync_folders
./sync.py --help
```

If you're on macOS, the stock rsync is 2.6.9 (from 2006). Most flags work, but `brew install rsync` is recommended.

## Usage — CLI

```bash
# Dry-run (default — shows what would happen)
./sync.py /path/to/src /path/to/dst --mode mirror

# Actually transfer
./sync.py /path/to/src /path/to/dst --mode mirror --execute

# Over SSH
./sync.py ./local user@host:/remote --mode backup --execute \
    --ssh-port 2222 --ssh-key ~/.ssh/id_ed25519 --bwlimit 10M

# Bidirectional sync (newer file wins)
./sync.py ./local user@host:/remote --mode sync --execute

# Sync to a Synology / NAS (skip permissions, ignore @eaDir/.DS_Store, 1s mtime tolerance)
./sync.py ./Photos user@nas:/volume1/photos --mode mirror --execute \
    --no-perms --nas-excludes --modify-window 1
```

Modes:

| Mode | Behaviour |
|---|---|
| `mirror` | Make DST identical to SRC. Deletes anything DST has that SRC doesn't (`rsync --delete`). |
| `backup` | Copy new/changed from SRC to DST. Never deletes on DST. |
| `sync` | Bidirectional; newer mtime wins. No deletes (`rsync --update` in both directions). |

Endpoints can be local paths, `[user@]host:/path` (SSH), or `rsync://...` (rsync daemon). The wrapper auto-adds `--compress` and builds `-e ssh ...` when either side is remote.

NAS-friendly options (off by default to keep behaviour predictable):

| Flag | Effect |
|---|---|
| `--no-perms` | Use `-rlt` instead of `-a` — skip permissions/owner/group. Cross-filesystem syncs (macOS ↔ NAS, FAT, exFAT) stop churning on bits the destination can't represent. |
| `--nas-excludes` | Adds `.DS_Store`, `@eaDir/`, `#recycle/` to excludes. |
| `--modify-window N` | Tolerate `N`-second mtime drift. `1` is right for FAT/exFAT and most NAS units. |
| `--exclude PATTERN` | Standard rsync exclude. Repeat for multiple. |

## Usage — Compare

```bash
./compare.py /path/to/src /path/to/dst                       # local vs local
./compare.py ./local user@host:/remote                       # local vs SSH
./compare.py user@host:/a user@host:/b --ssh-key ~/.ssh/id   # remote↔remote on same host (single ssh hop)
./compare.py ./a ./b --checksum --nas-excludes               # exact compare, ignore NAS noise
```

Read-only. Reports three categories — `[src only]`, `[dst only]`, `[modified]` — by parsing rsync's `--itemize-changes` output. Defaults to mtime+size; pass `--checksum` for exact-content comparison. `--limit N` caps each category (0 = no limit).

## Usage — GUI

```bash
./gui.py
```

Form for source, dest, mode, dry-run/execute toggle, and SSH/network options. Run streams rsync output line-by-line into a log pane. Cancel sends `SIGTERM` to the process group so the child rsync dies cleanly.

### `ModuleNotFoundError: No module named '_tkinter'`?

Your Python was built without Tk. `gui.py` prints remediation steps when this happens.

**Quick fix on macOS:** `/usr/bin/python3 gui.py` — the system Python ships with Tk.

**Proper fix for pyenv on macOS** (rebuild Python with Tk linked in):

```bash
brew install tcl-tk
TCLTK="$(brew --prefix tcl-tk)"
PKG_CONFIG_PATH="${TCLTK}/lib/pkgconfig:${PKG_CONFIG_PATH}" \
  CPPFLAGS="-I${TCLTK}/include/tcl-tk" \
  LDFLAGS="-L${TCLTK}/lib" \
  PYTHON_CONFIGURE_OPTS="--enable-shared" \
  pyenv install -f 3.13.7   # or whatever version you use
```

The pkg-config route is what actually works. Don't try `PYTHON_CONFIGURE_OPTS="--with-tcltk-includes='...' --with-tcltk-libs='...'"` — `python-build` does plain variable expansion (not `eval`), so the inner quotes don't survive and configure rejects the value. With `PKG_CONFIG_PATH` set, configure's own pkg-config probe finds tcl/tk and builds the link line itself.

`pyenv install -f` wipes site-packages. Snapshot first: `pip freeze > pkgs.txt`, then after rebuild: `pip install -r pkgs.txt`.

**Linux:** install your distro's `python3-tk` (`apt`), `python3-tkinter` (`dnf`), or `tk` (`pacman`) package — no rebuild needed.

## Logs

Per-platform default:

- macOS: `~/Library/Logs/sync_folders/sync.log`
- Linux: `$XDG_STATE_HOME/sync_folders/sync.log` (or `~/.local/state/sync_folders/sync.log`)

Override with `--log-file PATH`. Every run appends a timestamped block; nothing is ever rotated or truncated automatically.

## Tests

End-to-end naming-robustness suite:

```bash
python3 tests/test_naming.py
```

Builds a torture tree — spaces, mixed case, apostrophes, `& $ # @ ; ,`, `[]{}()`, Latin accents (`café`), CJK (`日本語`), emoji (`🚀`), trailing dots, leading dashes, hidden files, nested paths-with-spaces, and a temp dir whose own name contains a space — then runs all three modes with content verification. Filenames compare in NFC because APFS stores NFD and ext4 stores raw bytes; without normalization the same file fails equality across filesystems.

## Limitations

- **Two-way conflict resolution is mtime-based** with second resolution. Near-simultaneous edits on both sides can drop one. If that's a problem, use [unison](https://www.cis.upenn.edu/~bcpierce/unison/) instead.
- **Remote ↔ remote sync is rejected** (rsync limitation, not ours).
- **Local paths with a colon before the first `/`** will be misclassified as SSH endpoints (`notes:scratch` looks like `host:path`). Workaround: `./notes:scratch` or absolute paths.
- **No state file.** This is a thin wrapper, not a sync engine.

## Layout

```
sync.py             # sync CLI, stdlib only
compare.py          # read-only directory comparator, stdlib only
gui.py              # tkinter GUI, stdlib only
tests/test_naming.py
CLAUDE.md           # design notes for future contributors / Claude Code
```

## License

MIT.

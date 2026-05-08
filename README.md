# sync_folders

A small, stdlib-only Python wrapper around `rsync` — plus a tkinter GUI front-end. Syncs folder pairs locally or over the network, with a paranoid dry-run default and an end-to-end naming-robustness test suite.

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
./sync.py ./local user@host:/remote --mode additive --execute \
    --ssh-port 2222 --ssh-key ~/.ssh/id_ed25519 --bwlimit 10M

# Two-way (newer file wins)
./sync.py ./local user@host:/remote --mode two-way --execute
```

Modes:

| Mode | Behaviour |
|---|---|
| `mirror` | source → dest, dest becomes exact copy (`rsync --delete`) |
| `additive` | source → dest, never delete on dest |
| `two-way` | bidirectional, newer file wins (`rsync --update` in both directions) |

Endpoints can be local paths, `[user@]host:/path` (SSH), or `rsync://...` (rsync daemon). The wrapper auto-adds `--compress` and builds `-e ssh ...` when either side is remote.

## Usage — GUI

```bash
./gui.py
```

Form for source, dest, mode, dry-run/execute toggle, and SSH/network options. Run streams rsync output line-by-line into a log pane. Cancel sends `SIGTERM` to the process group so the child rsync dies cleanly.

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
sync.py             # CLI, ~230 lines, stdlib only
gui.py              # tkinter GUI, ~210 lines, stdlib only
tests/test_naming.py
CLAUDE.md           # design notes for future contributors / Claude Code
```

## License

MIT.

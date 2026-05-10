#!/usr/bin/env python3
"""Compare two directory trees using rsync's dry-run itemized output.

Read-only. Reports three categories:
  [src only]  files present in source, missing in dest
  [dst only]  files present in dest, missing in source
  [modified]  files in both but differing by size or mtime (or checksum with -c)

Endpoints can be local paths, [user@]host:/path (SSH), or rsync://... (rsync daemon).
Same-host remote/remote pairs are detected and the rsync runs over a single ssh hop.

Stdlib only.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from sync import NAS_EXCLUDES, Endpoint, build_ssh_e, parse_endpoint, rsync_version


def _color_enabled(stream) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return hasattr(stream, "isatty") and stream.isatty()


class C:
    """ANSI escape codes — empty strings if color is disabled."""

    def __init__(self, enabled: bool) -> None:
        if enabled:
            self.reset = "\033[0m"
            self.dim = "\033[2m"
            self.red = "\033[31m"
            self.green = "\033[32m"
            self.yellow = "\033[33m"
            self.blue = "\033[34m"
            self.magenta = "\033[35m"
            self.cyan = "\033[36m"
        else:
            self.reset = self.dim = self.red = self.green = ""
            self.yellow = self.blue = self.magenta = self.cyan = ""


def parse_itemized(output: str) -> dict[str, list[str]]:
    """Parse rsync -ni output into src_only / dst_only / modified lists.

    Itemized format: 11 flag chars, two spaces, path. Examples:
      >f+++++++++ new/file.txt        — new file (src only)
      >f.st...... existing/file.txt   — modified (size + time differ)
      *deleting   gone/file.txt       — would be deleted (dst only, with --delete)
      cd+++++++++ newdir/             — new directory
    """
    changes: dict[str, list[str]] = {"src_only": [], "dst_only": [], "modified": []}

    for line in output.splitlines():
        if not line.strip():
            continue

        if line.startswith("*deleting"):
            parts = line.split(None, 1)
            if len(parts) == 2:
                changes["dst_only"].append(parts[1].strip())
            continue

        # Skip rsync's chatty headers ("sending incremental file list", "sent X bytes").
        parts = line.split(" ", 1)
        if len(parts) < 2:
            continue
        flags, name = parts[0], parts[1].strip()
        if not name or len(flags) < 2:
            continue

        kind = flags[0]
        # > and < = transfer; c = create; h = hardlink. Skip everything else.
        if kind not in (">", "<", "c"):
            continue

        # "+++++++++" in the remaining flags means new file (every attribute differs
        # because there's nothing to compare against).
        if "+++++++++" in flags:
            changes["src_only"].append(name)
        else:
            changes["modified"].append(name)

    return changes


def detect_same_host_remote(src: Endpoint, dst: Endpoint) -> tuple[str, str, str] | None:
    """If both endpoints are SSH-style (not rsync://) on the same host, return
    (host, src_path, dst_path). Otherwise None."""
    if not (src.is_remote and dst.is_remote):
        return None
    if src.raw.startswith("rsync://") or dst.raw.startswith("rsync://"):
        return None
    s_host, _, s_path = src.raw.partition(":")
    d_host, _, d_path = dst.raw.partition(":")
    if s_host == d_host and s_path and d_path:
        return (s_host, s_path, d_path)
    return None


def build_compare_cmd(
    src: Endpoint,
    dst: Endpoint,
    *,
    checksum: bool,
    excludes: list[str],
    modify_window: int,
    ssh_e: str | None,
) -> list[str]:
    flags = [
        "rsync",
        "--dry-run",
        "--itemize-changes",
        "-r", "-t", "-l",         # recursive, preserve mtime, follow symlinks-as-symlinks
        "--delete",                # so we see "would-be-deleted" = dst-only files
        f"--modify-window={modify_window}",
    ]
    if checksum:
        flags.append("--checksum")
    if src.is_remote or dst.is_remote:
        flags.append("--compress")
    for exc in excludes:
        flags.extend(["--exclude", exc])

    same = detect_same_host_remote(src, dst)
    if same is not None:
        # Run rsync on the remote host; no -e ssh on the inner command.
        host, s_path, d_path = same
        if not s_path.endswith("/"):
            s_path += "/"
        if not d_path.endswith("/"):
            d_path += "/"
        inner = flags + [s_path, d_path]
        ssh = ["ssh"]
        # ssh_e was built for rsync's -e; reparse the args we care about.
        # Cheaper than re-plumbing: just take the same flags from caller-built string.
        if ssh_e:
            # ssh_e looks like: "ssh -p 2222 -i /path/to/key"
            ssh.extend(shlex.split(ssh_e)[1:])
        ssh.append(host)
        ssh.append(" ".join(shlex.quote(p) for p in inner))
        return ssh

    if ssh_e and (src.is_remote or dst.is_remote):
        flags.extend(["-e", ssh_e])

    s = src.rsync_arg(trailing_slash=True)
    d = dst.rsync_arg(trailing_slash=True)
    return flags + [s, d]


def print_section(title: str, files: list[str], color: str, prefix: str, limit: int, c: C) -> None:
    if not files:
        return
    n = len(files)
    print(f"{color}{title} ({n}){c.reset}")
    show = n if limit <= 0 else min(limit, n)
    for f in files[:show]:
        print(f"  {prefix} {f}")
    if show < n:
        print(f"  {c.dim}... and {n - show} more{c.reset}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("source", help="local path or [user@]host:path or rsync://...")
    p.add_argument("dest", help="local path or [user@]host:path or rsync://...")
    p.add_argument("--checksum", "-c", action="store_true",
                   help="compare by checksum (slower, exact)")
    p.add_argument("--limit", type=int, default=20,
                   help="max files to show per category; 0 = no limit")
    p.add_argument("--exclude", action="append", default=[],
                   help="rsync exclude pattern (repeatable)")
    p.add_argument("--nas-excludes", action="store_true",
                   help=f"add common NAS/macOS noise: {' '.join(NAS_EXCLUDES)}")
    p.add_argument("--modify-window", type=int, default=0,
                   help="seconds of mtime tolerance; use 1 for FAT/NAS filesystems")
    p.add_argument("--ssh-port", type=int, default=None)
    p.add_argument("--ssh-key", default=None, help="path to private key")
    p.add_argument("--ssh-opts", default=None,
                   help="extra ssh args, quoted (e.g. '-o StrictHostKeyChecking=no')")
    p.add_argument("--debug", action="store_true",
                   help="print the rsync command before running")
    args = p.parse_args(argv)

    if shutil.which("rsync") is None:
        print("error: rsync not found on PATH", file=sys.stderr)
        return 2
    ver = rsync_version()
    if ver and ver < (3, 0, 0):
        print(
            f"warning: rsync {'.'.join(map(str, ver))} is old (Apple ships 2.6.9). "
            "Comparison should still work but consider `brew install rsync`.",
            file=sys.stderr,
        )

    src = parse_endpoint(args.source)
    dst = parse_endpoint(args.dest)

    # Validate local sides exist; remote sides we let rsync complain about.
    if not src.is_remote and not Path(src.raw).is_dir():
        print(f"error: source is not a directory: {src.raw}", file=sys.stderr)
        return 2
    if not dst.is_remote and not Path(dst.raw).is_dir():
        print(f"warning: dest does not exist or is not a directory: {dst.raw}",
              file=sys.stderr)

    excludes = list(args.exclude)
    if args.nas_excludes:
        excludes.extend(NAS_EXCLUDES)

    ssh_e = build_ssh_e(args.ssh_port, args.ssh_key, args.ssh_opts)
    cmd = build_compare_cmd(
        src, dst,
        checksum=args.checksum,
        excludes=excludes,
        modify_window=args.modify_window,
        ssh_e=ssh_e,
    )

    c = C(_color_enabled(sys.stdout))

    print(f"{c.cyan}Comparing:{c.reset}")
    print(f"  source: {src.raw}")
    print(f"  dest:   {dst.raw}")
    if args.debug:
        print(f"  cmd:    {' '.join(shlex.quote(p) for p in cmd)}")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if result.returncode != 0:
        print(f"{c.red}rsync exited {result.returncode}{c.reset}", file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        return result.returncode

    changes = parse_itemized(result.stdout)

    print(f"\n{c.yellow}--- Comparison Results ---{c.reset}")
    print_section("[src only]",   changes["src_only"], c.green,   "+", args.limit, c)
    print_section("[dst only]",   changes["dst_only"], c.magenta, "-", args.limit, c)
    print_section("[modified]",   changes["modified"], c.blue,    "*", args.limit, c)

    total = sum(len(v) for v in changes.values())
    if total == 0:
        print(f"{c.green}Directories match.{c.reset}")
    else:
        print(f"\n{c.yellow}Summary:{c.reset} {total} difference(s)")
        print(f"  src only: {len(changes['src_only'])}")
        print(f"  dst only: {len(changes['dst_only'])}")
        print(f"  modified: {len(changes['modified'])}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

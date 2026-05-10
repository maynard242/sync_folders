#!/usr/bin/env python3
"""rsync wrapper for syncing folder pairs.

Endpoints can be local paths or remote specs:
  /path/to/dir                  local
  user@host:/path               SSH (rsync over ssh)
  host:/path                    SSH, current user
  rsync://user@host:873/module  rsync daemon

Modes:
  mirror    Make DST identical to SRC. Deletes anything DST has that SRC doesn't.
  backup    Copy new/changed from SRC to DST. Never deletes on DST.
  sync      Bidirectional; newer mtime wins. No deletes.
            (rsync --update both directions; local <-> remote OK,
            remote <-> remote not supported by rsync.)

Default is --dry-run. Pass --execute to actually transfer.
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

MODES = ("mirror", "backup", "sync")

# Synology / macOS noise that almost everyone wants ignored when syncing to a NAS.
NAS_EXCLUDES = (".DS_Store", "@eaDir/", "#recycle/")

# user@host:path  OR  host:path  (but not C:\... on Windows-style, and not a URL)
_SSH_RE = re.compile(r"^(?:[^@/:\s]+@)?[^/:\s]+:(?!//).+$")
_RSYNCD_RE = re.compile(r"^rsync://")


@dataclass(frozen=True)
class Endpoint:
    raw: str
    is_remote: bool

    def rsync_arg(self, *, trailing_slash: bool) -> str:
        s = str(Path(self.raw).resolve()) if not self.is_remote else self.raw
        if trailing_slash and not s.endswith("/"):
            s += "/"
        return s


def parse_endpoint(s: str) -> Endpoint:
    if _RSYNCD_RE.match(s) or _SSH_RE.match(s):
        return Endpoint(s, is_remote=True)
    return Endpoint(s, is_remote=False)


def default_log_file() -> Path:
    """Pick a log location that's idiomatic on each platform."""
    if platform.system() == "Darwin":
        return Path.home() / "Library/Logs/sync_folders/sync.log"
    # Linux / other POSIX: respect XDG_STATE_HOME
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    return Path(base) / "sync_folders/sync.log"


def rsync_version() -> tuple[int, int, int] | None:
    """Return rsync's (major, minor, patch) or None if unparseable."""
    rsync = shutil.which("rsync")
    if rsync is None:
        return None
    try:
        out = subprocess.run([rsync, "--version"], capture_output=True, text=True, timeout=5).stdout
    except (subprocess.TimeoutExpired, OSError):
        return None
    m = re.search(r"version\s+(\d+)\.(\d+)(?:\.(\d+))?", out)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


def build_ssh_e(ssh_port: int | None, ssh_key: str | None, ssh_opts: str | None) -> str | None:
    """Build the value for rsync -e (ssh transport options)."""
    parts: list[str] = []
    if ssh_port:
        parts += ["-p", str(ssh_port)]
    if ssh_key:
        parts += ["-i", ssh_key]
    if ssh_opts:
        parts += shlex.split(ssh_opts)
    if not parts:
        return None
    return "ssh " + " ".join(shlex.quote(p) for p in parts)


def build_rsync(
    src: Endpoint,
    dst: Endpoint,
    mode: str,
    *,
    dry_run: bool,
    compress: bool,
    bwlimit: str | None,
    ssh_e: str | None,
    excludes: list[str] | None = None,
    modify_window: int = 0,
    no_perms: bool = False,
) -> list[list[str]]:
    # Default: -a (archive). With --no-perms we drop -p/-o/-g (and the device/special
    # bits in -D) so cross-filesystem syncs (macOS ↔ NAS, FAT, exFAT) don't churn on
    # permission/ownership diffs the destination filesystem can't represent anyway.
    archive = "-a" if not no_perms else "-rlt"
    base = ["rsync", archive, "--human-readable", "--itemize-changes"]
    if dry_run:
        base.append("--dry-run")
    if compress or src.is_remote or dst.is_remote:
        # Compression is essentially free win on the wire; cheap-or-neutral locally.
        base.append("--compress")
    if bwlimit:
        base.append(f"--bwlimit={bwlimit}")
    if modify_window:
        base.append(f"--modify-window={modify_window}")
    for exc in excludes or ():
        base += ["--exclude", exc]
    if ssh_e and (src.is_remote or dst.is_remote):
        base += ["-e", ssh_e]

    s = src.rsync_arg(trailing_slash=True)
    d = dst.rsync_arg(trailing_slash=True)

    if mode == "mirror":
        return [base + ["--delete", s, d]]
    if mode == "backup":
        return [base + [s, d]]
    if mode == "sync":
        if src.is_remote and dst.is_remote:
            raise ValueError("sync mode with both endpoints remote is not supported by rsync")
        update = base + ["--update"]
        return [update + [s, d], update + [d, s]]
    raise ValueError(f"unknown mode: {mode}")


def setup_logging(log_file: Path) -> logging.Logger:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("sync_folders")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(log_file)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(sh)
    return logger


def run(cmd: list[str], logger: logging.Logger) -> int:
    logger.info("run: %s", " ".join(shlex.quote(c) for c in cmd))
    # Stream line-by-line so the GUI / tail -f see progress in real time.
    # errors="replace" — Apple's rsync 2.6.9 can emit non-UTF-8 bytes for
    # exotic filenames (emoji, CJK). Don't crash the wrapper over output decoding.
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, errors="replace",
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        logger.info(line.rstrip())
    return proc.wait()


def validate_endpoint(ep: Endpoint, *, role: str, create_if_missing: bool) -> str | None:
    """Return an error string, or None if OK."""
    if ep.is_remote:
        return None  # can't introspect a remote path without round-tripping; let rsync complain
    p = Path(ep.raw)
    if role == "source":
        if not p.is_dir():
            return f"source is not a directory: {p}"
        return None
    # dest
    if p.exists() and not p.is_dir():
        return f"dest exists and is not a directory: {p}"
    if not p.exists() and create_if_missing:
        p.mkdir(parents=True)
    return None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("source", help="local path or [user@]host:path or rsync://...")
    p.add_argument("dest", help="local path or [user@]host:path or rsync://...")
    p.add_argument("--mode", choices=MODES, default="backup")
    p.add_argument("--execute", action="store_true", help="actually transfer (default is dry-run)")
    p.add_argument("--log-file", type=Path, default=None)
    p.add_argument("--ssh-port", type=int, default=None)
    p.add_argument("--ssh-key", default=None, help="path to private key")
    p.add_argument("--ssh-opts", default=None, help="extra ssh args, quoted (e.g. '-o StrictHostKeyChecking=no')")
    p.add_argument("--compress", action="store_true", help="force --compress even for local syncs")
    p.add_argument("--bwlimit", default=None, help="rsync --bwlimit value, e.g. 10M")
    p.add_argument("--exclude", action="append", default=[], help="rsync exclude pattern (repeatable)")
    p.add_argument("--nas-excludes", action="store_true",
                   help=f"add common NAS/macOS noise: {' '.join(NAS_EXCLUDES)}")
    p.add_argument("--modify-window", type=int, default=0,
                   help="seconds of mtime tolerance; use 1 for FAT/exFAT/NAS filesystems")
    p.add_argument("--no-perms", action="store_true",
                   help="skip permissions/owner/group (use -rlt instead of -a) — recommended for NAS")
    args = p.parse_args(argv)

    ver = rsync_version()
    if ver is None:
        print("error: rsync not found or unrunnable on PATH", file=sys.stderr)
        return 2
    if ver < (3, 0, 0):
        print(
            f"warning: rsync {'.'.join(map(str, ver))} is old (Apple ships 2.6.9). "
            "Most flags still work; consider `brew install rsync` for newer features.",
            file=sys.stderr,
        )

    src = parse_endpoint(args.source)
    dst = parse_endpoint(args.dest)
    create_dest = not dst.is_remote
    for ep, role in ((src, "source"), (dst, "dest")):
        err = validate_endpoint(ep, role=role, create_if_missing=(role == "dest" and create_dest))
        if err:
            print(f"error: {err}", file=sys.stderr)
            return 2

    log_file = args.log_file or default_log_file()
    logger = setup_logging(log_file)
    dry = not args.execute
    logger.info(
        "sync start: %s -> %s mode=%s dry_run=%s rsync=%s platform=%s",
        src.raw, dst.raw, args.mode, dry, ".".join(map(str, ver)), platform.platform(),
    )

    ssh_e = build_ssh_e(args.ssh_port, args.ssh_key, args.ssh_opts)
    excludes = list(args.exclude)
    if args.nas_excludes:
        excludes.extend(NAS_EXCLUDES)
    try:
        cmds = build_rsync(
            src, dst, args.mode,
            dry_run=dry, compress=args.compress, bwlimit=args.bwlimit, ssh_e=ssh_e,
            excludes=excludes, modify_window=args.modify_window, no_perms=args.no_perms,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    rc = 0
    for cmd in cmds:
        rc = run(cmd, logger) or rc

    logger.info("sync end: rc=%s at %s", rc, datetime.now().isoformat(timespec="seconds"))
    if dry:
        logger.info("dry-run only — pass --execute to transfer")
    return rc


if __name__ == "__main__":
    sys.exit(main())

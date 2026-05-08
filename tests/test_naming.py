#!/usr/bin/env python3
"""Cross-platform naming-robustness tests for sync.py.

Builds a torture-test source tree (spaces, mixed case, punctuation, unicode,
hidden files, deep nesting) and runs sync.py end-to-end in each mode.
Verifies that every file made it across with content + name intact.

Stdlib only. Run: python3 tests/test_naming.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
SYNC_PY = HERE.parent / "sync.py"

# Names chosen to exercise common cross-platform pitfalls.
# Excluded:
#   - colon ":"   — HFS+/APFS path separator quirk + would match _SSH_RE
#   - newline     — pathological, not a real-world filename
#   - control chars
TRICKY_FILES: list[tuple[str, str]] = [
    ("plain.txt", "plain"),
    ("with spaces.txt", "spaces"),
    ("MixedCase.TXT", "mixed-case"),
    ("ALLCAPS.TXT", "allcaps"),
    ("apostrophe's file.txt", "apostrophe"),
    ("weird & file (1).txt", "ampersand-parens"),
    ("foo[bar]{baz}.txt", "brackets"),
    ("semi;colon,comma.txt", "punctuation"),
    ("dollar$hash#at@.txt", "shell-metas"),
    ("café.txt", "latin-accent"),
    ("日本語.txt", "cjk"),
    ("emoji_🚀_rocket.txt", "emoji"),
    ("trailing.dots...txt", "trailing-dots"),
    ("-leading-dash.txt", "leading-dash"),
    (".hidden", "hidden"),
    (".hidden with spaces", "hidden-spaces"),
    ("two   spaces.txt", "multi-space"),
]

TRICKY_DIRS = [
    "Sub Dir With Spaces",
    "MixedCase Dir",
    "café_dir",
    "deeply/nested/sub dir/",
]


def build_tree(root: Path) -> dict[str, str]:
    """Create the torture tree under root. Returns {relpath: content}."""
    expected: dict[str, str] = {}
    for name, content in TRICKY_FILES:
        p = root / name
        p.write_text(content, encoding="utf-8")
        expected[name] = content
    for d in TRICKY_DIRS:
        sub = root / d
        sub.mkdir(parents=True, exist_ok=True)
        for name, content in TRICKY_FILES[:5]:  # subset in each subdir
            p = sub / name
            p.write_text(f"{d}/{content}", encoding="utf-8")
            expected[str(Path(d) / name)] = f"{d}/{content}"
    return expected


def collect_tree(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in root.rglob("*"):
        if p.is_file():
            rel = p.relative_to(root).as_posix()
            # Normalize unicode form: APFS stores NFD, ext4 stores whatever you wrote.
            # Comparing in NFC neutralizes that.
            rel_nfc = unicodedata.normalize("NFC", rel)
            out[rel_nfc] = p.read_text(encoding="utf-8")
    return out


def normalize_keys(d: dict[str, str]) -> dict[str, str]:
    return {unicodedata.normalize("NFC", k): v for k, v in d.items()}


def run_sync(src: Path, dst: Path, mode: str, *, execute: bool = True) -> tuple[int, str]:
    argv = [sys.executable, str(SYNC_PY), str(src), str(dst), "--mode", mode]
    if execute:
        argv.append("--execute")
    proc = subprocess.run(argv, capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


# ── individual tests ────────────────────────────────────────────────────

def test_mode(mode: str, base: Path) -> tuple[bool, str]:
    src = base / "src"
    dst = base / "dst"
    src.mkdir()
    dst.mkdir()
    expected = build_tree(src)

    rc, out = run_sync(src, dst, mode, execute=True)
    if rc != 0:
        return False, f"rsync rc={rc}\n{out}"

    actual = collect_tree(dst)
    expected_nfc = normalize_keys(expected)

    missing = set(expected_nfc) - set(actual)
    extra = set(actual) - set(expected_nfc)
    bad_content = {k for k in expected_nfc.keys() & actual.keys() if expected_nfc[k] != actual[k]}

    if missing or extra or bad_content:
        msg = []
        if missing:
            msg.append(f"missing on dest: {sorted(missing)}")
        if extra:
            msg.append(f"unexpected on dest: {sorted(extra)}")
        if bad_content:
            msg.append(f"content mismatch: {sorted(bad_content)}")
        return False, "\n".join(msg) + f"\n--- rsync stdout ---\n{out}"
    return True, f"{len(expected_nfc)} files synced cleanly"


def test_path_with_spaces(base: Path) -> tuple[bool, str]:
    """Source/dest paths themselves contain spaces and mixed case."""
    src = base / "My Source Dir"
    dst = base / "Some DEST place"
    src.mkdir()
    dst.mkdir()
    (src / "hello world.txt").write_text("hi", encoding="utf-8")
    rc, out = run_sync(src, dst, "additive", execute=True)
    if rc != 0:
        return False, f"rc={rc}\n{out}"
    target = dst / "hello world.txt"
    if not target.is_file() or target.read_text(encoding="utf-8") != "hi":
        return False, f"expected file missing or wrong content at {target}\n{out}"
    return True, "paths-with-spaces handled"


def test_dry_run_makes_no_changes(base: Path) -> tuple[bool, str]:
    src = base / "src"
    dst = base / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "should not appear.txt").write_text("x", encoding="utf-8")
    rc, _ = run_sync(src, dst, "mirror", execute=False)
    if rc != 0:
        return False, f"dry-run rc={rc}"
    if any(dst.iterdir()):
        return False, f"dry-run wrote files to dest: {list(dst.iterdir())}"
    return True, "dry-run truly is dry"


def test_two_way_newer_wins(base: Path) -> tuple[bool, str]:
    a = base / "a"
    b = base / "b"
    a.mkdir()
    b.mkdir()
    fa = a / "file.txt"
    fb = b / "file.txt"
    fa.write_text("OLD on A", encoding="utf-8")
    fb.write_text("NEW on B", encoding="utf-8")
    # Make B newer.
    os.utime(fa, (1_700_000_000, 1_700_000_000))
    os.utime(fb, (1_700_000_500, 1_700_000_500))

    rc, out = run_sync(a, b, "two-way", execute=True)
    if rc != 0:
        return False, f"rc={rc}\n{out}"
    if fa.read_text() != "NEW on B" or fb.read_text() != "NEW on B":
        return False, f"expected both to have 'NEW on B', got A={fa.read_text()!r} B={fb.read_text()!r}"
    return True, "two-way: newer wins"


# ── runner ──────────────────────────────────────────────────────────────

def main() -> int:
    cases: list[tuple[str, callable]] = [
        ("additive mode (torture tree)", lambda b: test_mode("additive", b)),
        ("mirror mode (torture tree)", lambda b: test_mode("mirror", b)),
        ("two-way mode (torture tree)", lambda b: test_mode("two-way", b)),
        ("paths with spaces & caps", test_path_with_spaces),
        ("dry-run safety", test_dry_run_makes_no_changes),
        ("two-way: newer wins", test_two_way_newer_wins),
    ]
    failures = 0
    for name, fn in cases:
        with tempfile.TemporaryDirectory(prefix="sf_test ") as tmp:  # space in tmp dir name on purpose
            ok, msg = fn(Path(tmp))
        mark = "✓" if ok else "✗"
        print(f"  {mark} {name}: {msg}")
        if not ok:
            failures += 1
    print()
    print(f"{len(cases) - failures}/{len(cases)} passed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Tkinter front-end for sync.py.

Spawns sync.py as a subprocess and streams stdout into the log pane.
Stdlib-only. Runs on macOS and Linux (Tk ships with python on both).
"""

from __future__ import annotations

import os
import queue
import shlex
import signal
import subprocess
import sys
import threading
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from tkinter.scrolledtext import ScrolledText
except ImportError as _e:
    sys.stderr.write(
        f"error: this Python ({sys.executable}) was built without Tk support: {_e}\n"
        "\n"
        "Fixes:\n"
        "  • macOS quick: run with system Python — `/usr/bin/python3 gui.py`\n"
        "  • macOS pyenv (proper fix — rebuild Python against Homebrew tcl-tk):\n"
        "      brew install tcl-tk\n"
        '      TCLTK="$(brew --prefix tcl-tk)"\n'
        '      PKG_CONFIG_PATH="${TCLTK}/lib/pkgconfig:${PKG_CONFIG_PATH}" \\\n'
        '        CPPFLAGS="-I${TCLTK}/include/tcl-tk" \\\n'
        '        LDFLAGS="-L${TCLTK}/lib" \\\n'
        '        PYTHON_CONFIGURE_OPTS="--enable-shared" \\\n'
        "        pyenv install -f <your-version>\n"
        "    (Don't try to pass --with-tcltk-includes/libs through PYTHON_CONFIGURE_OPTS;\n"
        "     python-build doesn't eval the value, so embedded quotes/spaces break.)\n"
        "  • Linux (Debian/Ubuntu): sudo apt install python3-tk\n"
        "  • Linux (Fedora):       sudo dnf install python3-tkinter\n"
        "  • Linux (Arch):         sudo pacman -S tk\n"
    )
    sys.exit(2)

HERE = Path(__file__).resolve().parent
SYNC_PY = HERE / "sync.py"
MODES = ("backup", "mirror", "sync")
POLL_MS = 80


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("sync_folders")
        root.minsize(720, 520)

        self.proc: subprocess.Popen[str] | None = None
        self.q: queue.Queue[str | None] = queue.Queue()

        self._build_ui()
        self.root.after(POLL_MS, self._drain)

    # ── UI ────────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        pad = {"padx": 6, "pady": 4}
        frm = ttk.Frame(self.root)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        # Source / Dest
        self.src_var = tk.StringVar()
        self.dst_var = tk.StringVar()
        self._path_row(frm, 0, "Source", self.src_var)
        self._path_row(frm, 1, "Dest", self.dst_var)

        # Mode + execute
        self.mode_var = tk.StringVar(value="backup")
        self.exec_var = tk.BooleanVar(value=False)
        ttk.Label(frm, text="Mode").grid(row=2, column=0, sticky="w", **pad)
        mode_box = ttk.Combobox(frm, textvariable=self.mode_var, values=MODES, state="readonly", width=12)
        mode_box.grid(row=2, column=1, sticky="w", **pad)
        ttk.Checkbutton(frm, text="Execute (uncheck = dry-run)", variable=self.exec_var).grid(
            row=2, column=2, sticky="w", **pad
        )

        # SSH options (collapsible-ish: just always shown but compact)
        ssh_frm = ttk.LabelFrame(frm, text="SSH / network (only used when source or dest is remote)")
        ssh_frm.grid(row=3, column=0, columnspan=3, sticky="ew", padx=6, pady=6)
        ssh_frm.columnconfigure(1, weight=1)
        ssh_frm.columnconfigure(3, weight=1)

        self.port_var = tk.StringVar()
        self.key_var = tk.StringVar()
        self.opts_var = tk.StringVar()
        self.bw_var = tk.StringVar()
        self.compress_var = tk.BooleanVar(value=False)

        ttk.Label(ssh_frm, text="Port").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(ssh_frm, textvariable=self.port_var, width=8).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(ssh_frm, text="Key").grid(row=0, column=2, sticky="w", **pad)
        key_row = ttk.Frame(ssh_frm)
        key_row.grid(row=0, column=3, sticky="ew", **pad)
        key_row.columnconfigure(0, weight=1)
        ttk.Entry(key_row, textvariable=self.key_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(key_row, text="…", width=3, command=self._browse_key).grid(row=0, column=1)

        ttk.Label(ssh_frm, text="Extra ssh opts").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(ssh_frm, textvariable=self.opts_var).grid(row=1, column=1, columnspan=3, sticky="ew", **pad)

        ttk.Label(ssh_frm, text="Bandwidth limit").grid(row=2, column=0, sticky="w", **pad)
        ttk.Entry(ssh_frm, textvariable=self.bw_var, width=10).grid(row=2, column=1, sticky="w", **pad)
        ttk.Checkbutton(ssh_frm, text="Force --compress", variable=self.compress_var).grid(
            row=2, column=2, columnspan=2, sticky="w", **pad
        )

        # Buttons
        btns = ttk.Frame(frm)
        btns.grid(row=4, column=0, columnspan=3, sticky="ew", padx=6, pady=4)
        self.run_btn = ttk.Button(btns, text="Run", command=self.start)
        self.run_btn.pack(side="left")
        self.cancel_btn = ttk.Button(btns, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=(6, 0))
        ttk.Button(btns, text="Clear log", command=self.clear_log).pack(side="left", padx=(6, 0))
        self.status = tk.StringVar(value="ready")
        ttk.Label(btns, textvariable=self.status, foreground="#666").pack(side="right")

        # Log pane
        self.log = ScrolledText(frm, height=18, wrap="none", font=("Menlo", 11))
        self.log.grid(row=5, column=0, columnspan=3, sticky="nsew", padx=6, pady=6)
        frm.rowconfigure(5, weight=1)
        self.log.configure(state="disabled")

    def _path_row(self, parent: ttk.Frame, row: int, label: str, var: tk.StringVar) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=6, pady=4)
        ttk.Entry(parent, textvariable=var).grid(row=row, column=1, sticky="ew", padx=6, pady=4)
        ttk.Button(parent, text="Browse…", command=lambda: self._browse_dir(var)).grid(
            row=row, column=2, sticky="w", padx=6, pady=4
        )

    def _browse_dir(self, var: tk.StringVar) -> None:
        path = filedialog.askdirectory(initialdir=var.get() or str(Path.home()))
        if path:
            var.set(path)

    def _browse_key(self) -> None:
        path = filedialog.askopenfilename(initialdir=str(Path.home() / ".ssh"))
        if path:
            self.key_var.set(path)

    # ── subprocess plumbing ──────────────────────────────────────────────
    def _build_argv(self) -> list[str] | None:
        src = self.src_var.get().strip()
        dst = self.dst_var.get().strip()
        if not src or not dst:
            messagebox.showerror("sync_folders", "source and dest are required")
            return None
        argv = [sys.executable, str(SYNC_PY), src, dst, "--mode", self.mode_var.get()]
        if self.exec_var.get():
            argv.append("--execute")
        if self.port_var.get().strip():
            argv += ["--ssh-port", self.port_var.get().strip()]
        if self.key_var.get().strip():
            argv += ["--ssh-key", self.key_var.get().strip()]
        if self.opts_var.get().strip():
            argv += ["--ssh-opts", self.opts_var.get().strip()]
        if self.bw_var.get().strip():
            argv += ["--bwlimit", self.bw_var.get().strip()]
        if self.compress_var.get():
            argv.append("--compress")
        return argv

    def start(self) -> None:
        if self.proc is not None:
            return
        argv = self._build_argv()
        if argv is None:
            return
        self._append(f"$ {' '.join(shlex.quote(a) for a in argv)}\n")
        try:
            self.proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                errors="replace",
                # New process group so cancel() can kill children too.
                start_new_session=True,
            )
        except OSError as e:
            messagebox.showerror("sync_folders", f"failed to launch: {e}")
            return
        self.run_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.status.set("running…")
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            self.q.put(line)
        rc = self.proc.wait()
        self.q.put(f"\n[exit {rc}]\n")
        self.q.put(None)  # sentinel

    def cancel(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                self.proc.terminate()
            self.status.set("cancelling…")

    def _drain(self) -> None:
        try:
            while True:
                item = self.q.get_nowait()
                if item is None:
                    self._on_finish()
                else:
                    self._append(item)
        except queue.Empty:
            pass
        self.root.after(POLL_MS, self._drain)

    def _on_finish(self) -> None:
        rc = self.proc.returncode if self.proc else None
        self.proc = None
        self.run_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.status.set(f"done (rc={rc})")

    def _append(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")


def main() -> int:
    if not SYNC_PY.exists():
        print(f"sync.py not found next to gui.py at {SYNC_PY}", file=sys.stderr)
        return 2
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

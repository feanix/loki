"""Thin tkinter view over app.App: renders the model and forwards input, nothing else.

Worker threads never touch Tk. They post to App.events; `_tick` (every 100 ms via after()) calls
App.pump() and redraws only when something changed. A file drop (dnd.py) only queues the paths inside
the window procedure; `_tick` hands them to App.add_files. `capture_window` grabs the window with Win32
PrintWindow so a screenshot never needs the window on screen or focused.
"""

from __future__ import annotations

import itertools
import queue
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from loki import __version__, core, dnd
from loki.about import about_text, icon_path
from loki.app import (
    COLUMNS,
    CONVERTER_LABELS,
    EMPTY_HINT,
    MAX_CHOICES,
    TOOL_MISSING_HINT,
    App,
    max_size_label,
)

TICK_MS = 100
ROW_COLOURS = {
    core.UP_TO_DATE: "#1b7a2b",
    core.NEEDS_CONVERTING: "#b36b00",
    core.FLAGGED: "#7a7a7a",
    core.MISSING: "#7a7a7a",
    core.NEEDS_TOOL: "#b36b00",
    "error": "#c01818",
}


class MainWindow:
    def __init__(self, app: App, root: tk.Tk | None = None):
        self.app = app
        self.root = root or tk.Tk()
        self.root.title(f"Loki {__version__}")
        icon = icon_path()
        if icon:
            try:
                self.root.iconbitmap(default=str(icon))
            except tk.TclError:
                pass  # a missing icon must never stop the window
        self.root.geometry("960x560")
        self.root.minsize(700, 360)
        # Row keys hold backslashes, spaces and braces that Tcl lists mangle, so tree items get plain ids.
        self._iid_of: dict[str, str] = {}
        self._key_of: dict[str, str] = {}
        self._iid_counter = itertools.count()
        self._drops: queue.Queue = queue.Queue()
        self._targets: list[dnd.DropTarget] = []
        self._build()
        self._install_drop()
        self._refresh()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(TICK_MS, self._tick)

    # --- layout ---------------------------------------------------------------------------------

    def _build(self) -> None:
        r = self.root
        pad = {"padx": 8, "pady": 4}
        self.top = ttk.Frame(r)
        self.top.pack(fill="x", **pad)
        ttk.Label(self.top, text="Converter:").pack(side="left")
        self._converter_of = {label: key for key, label in CONVERTER_LABELS.items()}
        self.converter_var = tk.StringVar(value=CONVERTER_LABELS[self.app.converter])
        self.converter_box = ttk.Combobox(
            self.top, textvariable=self.converter_var, values=list(CONVERTER_LABELS.values()),
            state="readonly", width=22,
        )  # fmt: skip
        self.converter_box.pack(side="left", padx=8)
        self.converter_box.bind("<<ComboboxSelected>>", self._on_converter)
        self.tool_row = ttk.Frame(r)
        self.tool_label = ttk.Label(self.tool_row, text=TOOL_MISSING_HINT, foreground="#c01818")
        self.tool_label.pack(side="left")
        ttk.Button(self.tool_row, text="Browse...", command=self.browse_tool).pack(side="left", padx=8)

        self.bulk = bulk = ttk.Frame(r)
        bulk.pack(fill="x", **pad)
        self.convert_btn = ttk.Button(bulk, text="Convert all", command=self.app.convert_all)
        self.convert_btn.pack(side="left")
        ttk.Button(bulk, text="All on", command=lambda: self.app.set_all(True)).pack(side="left", padx=(16, 0))
        ttk.Button(bulk, text="All off", command=lambda: self.app.set_all(False)).pack(side="left", padx=4)
        self.remove_btn = ttk.Button(bulk, text="Remove", command=self.remove_selected)
        self.remove_btn.pack(side="left", padx=(16, 0))
        ttk.Button(bulk, text="About", command=self.show_about).pack(side="right")

        mid = ttk.Frame(r)
        mid.pack(fill="both", expand=True, **pad)
        cols = ("bake", "file", "folder", "size", "max", "status")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="extended")
        widths = ((80, "center"), (190, "w"), (250, "w"), (80, "center"), (95, "center"), (240, "w"))
        for col, text, (width, anchor) in zip(cols, COLUMNS, widths, strict=True):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor, stretch=col in ("file", "folder", "status"))
        for status, colour in ROW_COLOURS.items():
            self.tree.tag_configure(status, foreground=colour)
        scroll = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.empty_label = ttk.Label(mid, text=EMPTY_HINT, foreground="#7a7a7a", font=("Segoe UI", 14))
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<space>", self._on_space)
        self.tree.bind("<Delete>", self._on_delete)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.max_menu = tk.Menu(r, tearoff=0)

        self.status_var = tk.StringVar()
        ttk.Label(r, textvariable=self.status_var, relief="sunken", anchor="w").pack(
            fill="x", side="bottom"
        )

    # --- drag and drop --------------------------------------------------------------------------

    def top_hwnd(self) -> int:
        return top_hwnd(self.root)

    def _install_drop(self) -> None:
        """Accept dropped files on the window and on every widget in it (a drop lands on the widget
        under the cursor, so each one needs the hook). Off Windows there is no drop; the window still works."""
        if sys.platform != "win32":
            return
        self.root.update_idletasks()  # realise the native windows so they have handles
        widgets = [self.root]

        def walk(w: tk.Misc) -> None:
            for child in w.winfo_children():
                widgets.append(child)
                walk(child)

        walk(self.root)
        hwnds = {self.top_hwnd(), *(int(w.winfo_id()) for w in widgets)}
        for h in sorted(hwnds):
            self._targets.append(dnd.install(h, self._drops.put))

    def _apply_drops(self) -> bool:
        paths: list[str] = []
        while True:
            try:
                paths += self._drops.get_nowait()
            except queue.Empty:
                break
        if paths:
            self.app.add_files(paths)
        return bool(paths)

    # --- input ----------------------------------------------------------------------------------

    def show_about(self) -> None:
        messagebox.showinfo("About Loki", about_text(), parent=self.root)

    def _on_converter(self, _event=None) -> None:
        self.app.set_converter(self._converter_of[self.converter_var.get()])
        self._refresh()

    def browse_tool(self) -> None:
        path = filedialog.askopenfilename(
            title="Locate ImageToPAA.exe", filetypes=[("ImageToPAA", "ImageToPAA.exe"), ("All", "*.*")]
        )
        if path:
            self.app.set_tool(path)
            self._refresh()

    def remove_selected(self) -> None:
        self.app.remove(self._selected_keys())
        self._refresh()

    def _selected_keys(self) -> list[str]:
        return [self._key_of[i] for i in self.tree.selection() if i in self._key_of]

    def _on_delete(self, _event=None) -> str:
        self.remove_selected()
        return "break"

    def set_row_max(self, key: str, value: int | None) -> None:
        """Menu action: only this row's cap changes."""
        self.app.set_max_size([key], value)
        self._refresh()

    def _on_click(self, event) -> str | None:
        if self.tree.identify_region(event.x, event.y) != "cell":
            return None
        key = self._key_of.get(self.tree.identify_row(event.y))
        if key is None:
            return None
        col = self.tree.identify_column(event.x)
        if col == "#1":
            self.app.toggle([key])
            self._refresh()
            return "break"
        if col == "#5":
            self.max_menu.delete(0, "end")
            for value in MAX_CHOICES:
                self.max_menu.add_command(
                    label=max_size_label(value), command=lambda v=value, k=key: self.set_row_max(k, v)
                )
            self.max_menu.tk_popup(event.x_root, event.y_root)
            return "break"
        return None

    def _on_select(self, _event=None) -> None:
        self.app.selection = self._selected_keys()
        self.status_var.set(self.app.summary())

    def _on_space(self, _event) -> str:
        self.app.toggle(self._selected_keys())
        self._refresh()
        return "break"

    # --- rendering ------------------------------------------------------------------------------

    def _tick(self) -> None:
        try:
            dropped = self._apply_drops()
            if self.app.pump() or dropped:
                self._refresh()
        finally:
            self.root.after(TICK_MS, self._tick)

    def _refresh(self) -> None:
        app = self.app
        self.convert_btn.state(["disabled"] if app.converting else ["!disabled"])
        self.converter_var.set(CONVERTER_LABELS[app.converter])
        if not app.tool_warning:
            self.tool_row.pack_forget()
        else:
            self.tool_row.pack(fill="x", padx=8, pady=4, before=self.bulk)  # re-pack is idempotent
        wanted = {row.key for row in app.rows}
        for key in [k for k in self._iid_of if k not in wanted]:
            iid = self._iid_of.pop(key)
            self._key_of.pop(iid, None)
            if self.tree.exists(iid):
                self.tree.delete(iid)
        for i, row in enumerate(app.rows):
            values = (
                row.bake_cell, row.name, row.folder_text, row.size_text, row.max_cell, row.status_text
            )
            iid = self._iid_of.get(row.key)
            if iid:
                self.tree.item(iid, values=values, tags=(row.status,))
                self.tree.move(iid, "", i)
            else:
                iid = f"r{next(self._iid_counter)}"
                self._iid_of[row.key], self._key_of[iid] = iid, row.key
                self.tree.insert("", i, iid=iid, values=values, tags=(row.status,))
        if app.rows:
            self.empty_label.place_forget()
        else:
            self.empty_label.place(relx=0.5, rely=0.45, anchor="center")
        self.status_var.set(app.summary())

    def close(self) -> None:
        for t in self._targets:
            t.uninstall()
        self.app.close()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def run_window(settings_path: str | None) -> None:
    MainWindow(App(settings_path)).run()


def top_hwnd(root: tk.Tk) -> int:
    """Tk's real top-level window is the parent of the client window winfo_id() names."""
    import ctypes

    user32 = ctypes.windll.user32
    user32.GetParent.restype, user32.GetParent.argtypes = ctypes.c_void_p, [ctypes.c_void_p]
    return user32.GetParent(root.winfo_id()) or root.winfo_id()


# --- screenshot -------------------------------------------------------------------------------------

def capture_window(root: tk.Tk, out: Path) -> None:
    """Render the window into `out` with PrintWindow (works for offscreen, unfocused windows)."""
    import ctypes
    from ctypes import wintypes

    from PIL import Image

    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    # Explicit argtypes: handles are 64-bit, and ctypes' default int conversion truncates them.
    for fn, res, args in (
        (user32.GetWindowRect, wintypes.BOOL, [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]),
        (user32.GetDC, wintypes.HDC, [wintypes.HWND]),
        (user32.ReleaseDC, ctypes.c_int, [wintypes.HWND, wintypes.HDC]),
        (user32.PrintWindow, wintypes.BOOL, [wintypes.HWND, wintypes.HDC, wintypes.UINT]),
        (gdi32.CreateCompatibleDC, wintypes.HDC, [wintypes.HDC]),
        (gdi32.CreateCompatibleBitmap, wintypes.HBITMAP, [wintypes.HDC, ctypes.c_int, ctypes.c_int]),
        (gdi32.SelectObject, wintypes.HGDIOBJ, [wintypes.HDC, wintypes.HGDIOBJ]),
        (gdi32.DeleteObject, wintypes.BOOL, [wintypes.HGDIOBJ]),
        (gdi32.DeleteDC, wintypes.BOOL, [wintypes.HDC]),
        (
            gdi32.GetDIBits,
            ctypes.c_int,
            [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
             ctypes.c_void_p, wintypes.UINT],
        ),
    ):
        fn.restype, fn.argtypes = res, args

    class BitmapInfoHeader(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]  # fmt: skip

    hwnd = top_hwnd(root)
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    screen = user32.GetDC(None)
    dc = gdi32.CreateCompatibleDC(screen)
    bmp = gdi32.CreateCompatibleBitmap(screen, w, h)
    old = gdi32.SelectObject(dc, bmp)
    try:
        if not user32.PrintWindow(hwnd, dc, 2):  # 2 = PW_RENDERFULLCONTENT
            raise OSError("PrintWindow failed")
        header = BitmapInfoHeader(ctypes.sizeof(BitmapInfoHeader), w, -h, 1, 32, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(dc, bmp, 0, h, buf, ctypes.byref(header), 0)
        Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB").save(out)
    finally:
        gdi32.SelectObject(dc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(dc)
        user32.ReleaseDC(None, screen)




def screenshot(
    settings_path: str | None, out: Path, timeout: float = 10.0, drop: list[str] | None = None, echo=print
) -> None:
    """Build the real window offscreen (never activated), optionally post it a real file drop, let it
    populate, print the digest, capture, exit."""
    import time

    if sys.platform != "win32":
        raise SystemExit("--screenshot needs Windows (PrintWindow)")
    app = App(settings_path, interval=0.1, settle=0.3)
    win = MainWindow(app)
    # Offscreen so the owner's desktop is never disturbed; no focus_force, no lift. A fully
    # offscreen window paints no client area under DWM; a near-transparent layered one does.
    win.root.attributes("-alpha", 0.01)
    win.root.geometry("960x560+-4000+-4000")
    win.root.update()
    if drop:
        # Same window procedure an Explorer drop goes through (self-test of the packaged exe).
        dnd.post_drop(win.top_hwnd(), [str(Path(p).absolute()) for p in drop])
        want = len(app.rows) + len({core.file_key(p) for p in drop if str(p).lower().endswith(".png")})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and len(app.rows) < want:
            win.root.update()
            time.sleep(0.05)
    for _ in range(10):
        win.root.update()
    echo(app.digest())
    echo(f"status: {app.summary()}")
    capture_window(win.root, out)
    win.close()

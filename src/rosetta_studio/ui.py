"""Shared presentation for the local Studio and read-only activity monitor."""
from __future__ import annotations

import re
import tkinter as tk
from tkinter import ttk

BG = "#f3f5f7"
PANEL = "#ffffff"
INK = "#20292f"
MUTED = "#687680"
LINE = "#dce3e8"
ACCENT = "#14785e"
BLUE = "#275c9a"
ERROR = "#b33c45"


def configure_style(master: tk.Misc) -> None:
    style = ttk.Style(master)
    style.theme_use("clam")
    style.configure("TCombobox", padding=6, fieldbackground=PANEL,
                    background=PANEL, foreground=INK, bordercolor=LINE,
                    font=("Segoe UI", 10))
    style.configure("TButton", padding=(10, 7), background=PANEL,
                    foreground=INK, bordercolor=LINE, font=("Segoe UI", 9))
    style.map("TButton", background=[("active", "#e9eef2")],
              foreground=[("disabled", MUTED)])
    style.configure("Accent.TButton", background=ACCENT, foreground=PANEL,
                    bordercolor=ACCENT)
    style.map("Accent.TButton", background=[("active", "#0f634d"),
                                            ("disabled", "#e1e7e5")],
              foreground=[("disabled", MUTED)])
    style.configure("TCheckbutton", background=BG, foreground=MUTED,
                    font=("Segoe UI", 9))
    style.map("TCheckbutton", background=[("active", BG)])
    style.configure("TPanedwindow", background=BG)
    style.configure("TScrollbar", background="#e8edf0", troughcolor=BG,
                    bordercolor=BG, arrowcolor=MUTED)
    style.configure("Activity.Treeview", background=PANEL, fieldbackground=PANEL,
                    foreground=INK, borderwidth=0, rowheight=30,
                    font=("Segoe UI", 9))
    style.configure("Activity.Treeview.Heading", background="#edf1f4",
                    foreground=MUTED, relief="flat", padding=(8, 8),
                    font=("Segoe UI", 9, "bold"))
    style.map("Activity.Treeview", background=[("selected", "#dcece5")],
              foreground=[("selected", INK)])


class CodePane(tk.Frame):
    """A text widget with synchronized line numbers and preservation markers."""
    def __init__(self, master: tk.Misc, *, readonly: bool = False):
        super().__init__(master, bg=PANEL)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)
        self.gutter = tk.Text(self, width=4, wrap="none", takefocus=False,
                              state="disabled", bg="#f5f7f9", fg="#8b98a2",
                              relief="flat", borderwidth=0, padx=8, pady=12,
                              font=("Cascadia Mono", 10), cursor="arrow")
        self.gutter.grid(row=0, column=0, sticky="ns")
        self.text = tk.Text(self, wrap="none", undo=not readonly, padx=14,
                            pady=12, bg=PANEL, fg=INK, insertbackground=ACCENT,
                            selectbackground="#dcece5", relief="flat",
                            borderwidth=0, font=("Cascadia Mono", 10), tabs=(32,),
                            state="disabled" if readonly else "normal")
        self.text.grid(row=0, column=1, sticky="nsew")
        ybar = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        ybar.grid(row=0, column=2, sticky="ns")
        xbar = ttk.Scrollbar(self, orient="horizontal", command=self.text.xview)
        xbar.grid(row=1, column=1, sticky="ew")
        self.text.configure(yscrollcommand=lambda a, b: self._scroll(a, b, ybar),
                            xscrollcommand=xbar.set)
        self.text.tag_configure("native", background="#edf6f1", foreground="#25644e")
        self.text.tag_configure("marker", foreground=ACCENT,
                                font=("Cascadia Mono", 10, "bold"))
        self.text.tag_configure("command", foreground=BLUE)
        self.text.bind("<<Modified>>", self._modified, add=True)
        self.help_enabled = False
        self.help_borders: list[tk.Frame] = []
        self.text.bind("<Configure>", lambda _event: self._draw_help(), add=True)
        self.pending: str | None = None
        self.after_idle(self.annotate)

    def _scroll(self, first: str, last: str, bar: ttk.Scrollbar) -> None:
        bar.set(first, last)
        self.gutter.yview_moveto(first)
        self._draw_help()

    def show_help(self, enabled: bool) -> None:
        self.help_enabled = enabled
        self.text.tag_configure("native", background="#fff0ef" if enabled else "#edf6f1",
                                foreground="#9c272b" if enabled else "#25644e")
        self._draw_help()

    def _draw_help(self) -> None:
        for border in self.help_borders:
            border.destroy()
        self.help_borders.clear()
        if not self.help_enabled:
            return
        ranges = self.text.tag_ranges("native")
        for start, end in zip(ranges[::2], ranges[1::2]):
            first = int(str(start).split(".")[0])
            last = int(self.text.index(f"{end}-1c").split(".")[0])
            visible = [self.text.dlineinfo(f"{line}.0") for line in range(first, last + 1)]
            visible = [box for box in visible if box is not None]
            if not visible:
                continue
            top = max(0, visible[0][1] - 2)
            bottom = min(self.text.winfo_height(), visible[-1][1] + visible[-1][3] + 2)
            width = max(0, self.text.winfo_width() - 4)
            for x, y, w, h in ((2, top, width, 2), (2, bottom - 2, width, 2),
                               (2, top, 2, bottom - top),
                               (width, top, 2, bottom - top)):
                border = tk.Frame(self, bg="#c52b32", takefocus=False)
                border.place(x=self.text.winfo_x() + x, y=self.text.winfo_y() + y,
                             width=w, height=h)
                self.help_borders.append(border)

    def _modified(self, _event: tk.Event) -> None:
        if self.text.edit_modified():
            self.text.edit_modified(False)
            if self.pending is None:
                self.pending = self.after_idle(self.annotate)

    def annotate(self) -> None:
        self.pending = None
        content = self.text.get("1.0", "end-1c")
        count = content.count("\n") + 1
        self.gutter.configure(state="normal", width=max(4, len(str(count)) + 1))
        self.gutter.delete("1.0", "end")
        self.gutter.insert("1.0", "\n".join(map(str, range(1, count + 1))))
        self.gutter.configure(state="disabled")
        for tag in ("native", "marker", "command"):
            self.text.tag_remove(tag, "1.0", "end")
        native = False
        for number, line in enumerate(content.splitlines(), 1):
            start, end = f"{number}.0", f"{number}.end"
            # These tags are visual annotations only, never parser decisions.
            marker = bool(re.search(r"@(?:END-)?[A-Z]+-PASSTHROUGH", line))
            if marker and "@END-" not in line:
                native = True
            if native:
                self.text.tag_add("native", start, end + "+1c")
            if marker:
                self.text.tag_add("marker", start, end)
            elif not native:
                for match in re.finditer(r"\\[A-Za-z]+|^#{1,2}[A-Za-z]+|^={1,3}(?=\s)", line):
                    self.text.tag_add("command", f"{number}.{match.start()}", f"{number}.{match.end()}")
            if marker and "@END-" in line:
                native = False
        self.gutter.yview_moveto(self.text.yview()[0])
        self._draw_help()

    def destroy(self) -> None:
        """Cancel deferred highlighting before Tk destroys the text widgets."""
        if self.pending is not None:
            try:
                self.after_cancel(self.pending)
            except tk.TclError:
                pass
            self.pending = None
        for border in self.help_borders:
            try:
                if border.winfo_exists():
                    border.destroy()
            except tk.TclError:
                pass
        self.help_borders.clear()
        super().destroy()

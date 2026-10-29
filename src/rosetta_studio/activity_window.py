"""Read-only live view of Rosetta activity.

Every Rosetta front end appends to one shared event table: the stdio MCP server
writes `source="MCP"` rows and the DeepSeek Harness plugin writes
`source="Harness"` rows. This window polls that table and shows both, so it is a
view of Rosetta activity rather than of any single front end.
"""
from __future__ import annotations

import sqlite3
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from .event_store import DEFAULT_DB, EventStore
from .ui import BG, INK, MUTED, LINE, ACCENT, BLUE, ERROR, PANEL, CodePane, configure_style


ACTION_LABELS = {
    "list_pairs": "Discover tools",
    "open": "Open + project",
    "create": "Create session",
    "read": "Read agent view",
    "write": "Edit agent view",
    "preview": "Preview delivery",
    "commit": "Commit delivery",
    "close": "Close session",
}

SOURCE_LABELS = {
    "MCP": "MCP",
    "Harness": "Harness plugin",
}

VIEW_FORMATS = {
    "LaTeX / Typst": ("Typst", "LaTeX"),
    "Verilog / SystemVerilog": ("SystemVerilog", "Verilog"),
    "PRQL / SQL": ("PRQL", "SQL"),
    "Bash / Nushell (experimental)": ("Nushell", "Bash"),
}


class ActivityWindow(tk.Toplevel):
    def __init__(self, master: tk.Misc, events_db: Path = DEFAULT_DB):
        super().__init__(master)
        self.title("Rosetta Agent Activity")
        self.geometry("1460x790")
        self.minsize(980, 620)
        self.configure(bg=BG)
        self.store = EventStore(events_db)
        self.last_id = 0
        self.follow_var = tk.BooleanVar(value=True)
        self.help_var = tk.BooleanVar(value=False)
        self.view_panes: list[CodePane] = []
        self.help_labels: list[tk.Label] = []
        self._automatic_selection = False
        self._selection_job: str | None = None
        self._event_rows: dict[str, dict] = {}
        self.session_var = tk.StringVar(value="All sessions")
        configure_style(self)
        self._build()
        self._load_recent()
        self._poll_job = self.after(350, self._poll)

    def _build(self) -> None:
        head = tk.Frame(self, bg=BG, padx=20, pady=14)
        head.pack(fill="x")
        tk.Label(head, text="Rosetta", bg=BG, fg=INK,
                 font=("Segoe UI", 20, "bold")).pack(side="left")
        tk.Label(head, text="AGENT ACTIVITY", bg=BG, fg=MUTED,
                 font=("Segoe UI", 10)).pack(side="left", padx=(14, 0))
        ttk.Checkbutton(head, text="Follow latest", variable=self.follow_var,
                        command=self._follow_latest).pack(side="right")
        self.help_button = ttk.Button(head, text="Show help", command=self._toggle_help)
        self.help_button.pack(side="right", padx=8)
        self.status_label = tk.Label(head, text="WAITING", bg="#e8edf1", fg=MUTED,
                                     font=("Segoe UI", 9, "bold"), padx=10, pady=5)
        self.status_label.pack(side="right", padx=16)

        self.context_var = tk.StringVar(
            value="No recorded activity")
        tk.Label(self, textvariable=self.context_var, bg=BG, fg=MUTED,
                 font=("Segoe UI", 9), anchor="w", padx=20).pack(fill="x")

        rail = tk.Frame(self, bg=BG, padx=20, pady=12)
        rail.pack(fill="x")
        self.stages: dict[str, tk.Label] = {}
        for index, (action, title) in enumerate((('open', 'Open / project'),
                                                ('read', 'Read'), ('write', 'Edit'),
                                                ('preview', 'Preview'),
                                                ('commit', 'Deliver'), ('close', 'Close'))):
            rail.columnconfigure(index, weight=1, uniform="stage")
            label = tk.Label(rail, text=f"{index + 1:02d}   {title}", bg="#e8edf1",
                             fg=MUTED, font=("Segoe UI", 9), anchor="w", padx=12, pady=8)
            label.grid(row=0, column=index, sticky="ew", padx=(0, 4))
            self.stages[action] = label

        split = ttk.Panedwindow(self, orient="vertical")
        split.pack(fill="both", expand=True, padx=20, pady=(10, 8))
        views = ttk.Panedwindow(split, orient="horizontal")
        split.add(views, weight=4)
        self.target_text = self._view(views, "Target view", "", ACCENT)
        self.agent_text = self._view(views, "Agent view", "", BLUE)

        timeline = tk.Frame(split, bg="#ffffff", highlightbackground=LINE,
                            highlightthickness=1)
        split.add(timeline, weight=1)
        timeline_head = tk.Frame(timeline, bg=PANEL, padx=12, pady=8)
        timeline_head.pack(fill="x")
        tk.Label(timeline_head, text="Activity timeline", bg=PANEL, fg=INK,
                 font=("Segoe UI", 10, "bold")).pack(side="left")
        self.session_box = ttk.Combobox(timeline_head, textvariable=self.session_var,
                                        state="readonly", width=25, values=['All sessions'])
        self.session_box.pack(side="right")
        self.session_box.bind('<<ComboboxSelected>>', lambda _e: self._filter_sessions())
        table_frame = tk.Frame(timeline, bg="#ffffff")
        table_frame.pack(fill="both", expand=True)
        columns = ("time", "source", "step", "status", "pair", "artifact")
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings",
                                 selectmode="browse", height=5, style="Activity.Treeview")
        headings = {
            "time": ("Date / time", 175), "source": ("Source", 120), "step": ("Step", 145),
            "status": ("Status", 80), "pair": ("Tool pair", 150),
            "artifact": ("Target file", 220),
        }
        for col, (title, width) in headings.items():
            self.table.heading(col, text=title)
            self.table.column(col, width=width, minwidth=60,
                              stretch=col in ('pair', 'artifact'), anchor="w")
        self.table.tag_configure("error", foreground=ERROR)
        self.table.tag_configure("success", foreground=INK)
        self.table.tag_configure("even", background="#f7f9fa")
        self.table.tag_configure("odd", background=PANEL)
        bar = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=bar.set)
        self.table.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.table.bind("<<TreeviewSelect>>", self._on_select)

        self.detail_var = tk.StringVar(value="")
        detail = tk.Label(self, textvariable=self.detail_var, bg=BG, fg=MUTED,
                 font=("Segoe UI", 9), anchor="w", padx=20, pady=7,
                 wraplength=1300, justify="left")
        self.detail_label = detail
        self.content_split = split
        self.bind('<Configure>', lambda e: detail.configure(wraplength=max(100, e.width - 40))
                  if e.widget is self else None)

    def _view(self, parent: ttk.Panedwindow, title: str, subtitle: str, color: str) -> tk.Text:
        frame = tk.Frame(parent, bg="#ffffff", highlightbackground=LINE,
                         highlightthickness=1)
        parent.add(frame, weight=1)
        tk.Frame(frame, bg=color, height=3).pack(fill="x")
        header = tk.Frame(frame, bg=PANEL, padx=12, pady=9)
        header.pack(fill="x")
        tk.Label(header, text=title, bg=PANEL, fg=INK,
                 font=("Segoe UI", 10, "bold"), anchor="w",
                 ).pack(side="left")
        format_label = tk.Label(header, text=subtitle, bg=PANEL, fg=color,
                                font=("Segoe UI", 9))
        format_label.pack(side="right")
        if title == 'Target view':
            self.target_format_label = format_label
            self.target_title_label = header.winfo_children()[0]
        else:
            self.agent_format_label = format_label
            self.agent_title_label = header.winfo_children()[0]
        help_label = tk.Label(frame, bg="#fff0ef", fg="#a6242b",
                              font=("Segoe UI", 10, "bold"), justify="left", anchor="w",
                              padx=12, pady=9)
        help_label.bind("<Configure>", lambda e: help_label.configure(
            wraplength=max(100, e.width - 24)))
        self.help_labels.append(help_label)
        pane = CodePane(frame, readonly=True)
        pane.pack(fill="both", expand=True)
        self.view_panes.append(pane)
        return pane.text

    def _toggle_help(self) -> None:
        enabled = not self.help_var.get()
        self.help_var.set(enabled)
        self.help_button.configure(text="Hide help" if enabled else "Show help")
        self.target_title_label.configure(
            text="New tool / Target view" if enabled else "Target view")
        self.agent_title_label.configure(
            text="Old tool / Agent view" if enabled else "Agent view")
        descriptions = (
            "NEW TOOL: delivered target code. Red blocks retain native syntax; "
            "unmarked regions are translated from the old tool.",
            "WRITE-THROUGH BLOCK: native new-tool code, carried without translation. "
            "Outside red blocks: editable old-tool code.",
        )
        for pane, label, description in zip(self.view_panes, self.help_labels, descriptions):
            if enabled:
                label.configure(text=description)
                label.pack(fill="x", before=pane)
            else:
                label.pack_forget()
            pane.show_help(enabled)
        if enabled:
            self.follow_var.set(False)
            # Show the largest block rather than a small preamble at the file top.
            for pane in self.view_panes:
                ranges = pane.text.tag_ranges("native")
                blocks = list(zip(ranges[::2], ranges[1::2]))
                if blocks:
                    start, _end = max(blocks, key=lambda block: int(
                        pane.text.count(block[0], block[1], "lines")[0]))
                    pane.text.yview(start)
            self.after_idle(lambda: [pane._draw_help() for pane in self.view_panes])

    @staticmethod
    def _replace(editor: tk.Text, content: str | None) -> None:
        editor.configure(state="normal")
        editor.delete("1.0", "end")
        if content:
            editor.insert("1.0", content)
        editor.yview_moveto(0)
        editor.xview_moveto(0)
        editor.configure(state="disabled")

    def _load_recent(self) -> None:
        self.last_id, events = self.store.recent_activity(100)
        for event in events:
            self._add_event(event)
        latest = self._latest_display_item()
        if latest:
            self._select_event(latest)

    def _latest_display_item(self) -> str | None:
        visible = self.table.get_children()
        for item in reversed(visible):
            event = self._event_rows[item]
            if event["target_path"]:
                full = self.store.get(int(item))
                if full and (full["target_view"] or full["agent_view"]):
                    return item
        return visible[-1] if visible else None

    def _add_event(self, event: dict) -> None:
        self.last_id = max(self.last_id, event["id"])
        self._event_rows[str(event['id'])] = event
        file_name = Path(event["target_path"]).name if event["target_path"] else ""
        source = event["source"] or ""
        self.table.insert("", "end", iid=str(event["id"]), values=(
            event["created_at"][:19].replace("T", " "),
            SOURCE_LABELS.get(source, source),
            ACTION_LABELS.get(event["action"], event["action"]),
            event["status"].capitalize(), event["pair_name"] or "", file_name,
        ), tags=('even' if event['id'] % 2 == 0 else 'odd', event["status"]))
        if len(self._event_rows) > 300:
            oldest = next(iter(self._event_rows))
            self.table.delete(oldest)
            del self._event_rows[oldest]
        sessions = sorted({row['session_id'] for row in self._event_rows.values() if row['session_id']})
        self.session_box.configure(values=['All sessions', *sessions])
        self._filter_sessions(select=False)

    def _filter_sessions(self, *, select: bool = True) -> None:
        selected = self.session_var.get()
        for item, event in self._event_rows.items():
            if selected == 'All sessions' or event['session_id'] == selected:
                self.table.move(item, '', 'end')
            else:
                self.table.detach(item)
        visible = self.table.get_children()
        if select:
            if visible:
                self._select_event(self._latest_display_item())
            else:
                self._replace(self.target_text, None)
                self._replace(self.agent_text, None)
                self.context_var.set('No events in selected session')

    def _select_event(self, item_id: str) -> None:
        self._automatic_selection = True
        self.table.selection_set(item_id)
        self.table.see(item_id)
        self._show_event(int(item_id))
        if self._selection_job:
            self.after_cancel(self._selection_job)
        self._selection_job = self.after_idle(self._finish_selection)

    def _finish_selection(self) -> None:
        self._automatic_selection = False
        self._selection_job = None

    def _show_event(self, event_id: int) -> None:
        event = self.store.get(event_id)
        if event is None:
            return
        self._replace(self.target_text, event["target_view"])
        self._replace(self.agent_text, event["agent_view"])
        session = (event["session_id"] or "")[:8]
        source = event["source"] or "Rosetta"
        pair = event['pair_name'] or 'Rosetta'
        target_format, agent_format = VIEW_FORMATS.get(pair, ('', ''))
        self.target_format_label.configure(text=target_format)
        self.agent_format_label.configure(text=agent_format)
        self.status_label.configure(text=event['status'].upper(),
                                    bg='#fbecef' if event['status'] == 'error' else '#e1f0e8',
                                    fg=ERROR if event['status'] == 'error' else ACCENT)
        action = 'open' if event['action'] == 'create' else event['action']
        for key, label in self.stages.items():
            active = key == action
            label.configure(bg=INK if active else '#e8edf1', fg=PANEL if active else MUTED)
        self.context_var.set(
            f"{SOURCE_LABELS.get(source, source)}  /  {pair}  /  "
            f"Session {session or '-'}  /  {Path(event['target_path']).name if event['target_path'] else 'No target file'}"
        )
        self.detail_var.set(event["detail"])
        if event['status'] == 'error':
            self.detail_label.pack(fill="x", side="bottom", before=self.content_split)
        else:
            self.detail_label.pack_forget()

    def _on_select(self, _event: tk.Event) -> None:
        if self._automatic_selection:
            return
        selection = self.table.selection()
        if selection:
            self.follow_var.set(False)
            self._show_event(int(selection[0]))

    def _follow_latest(self) -> None:
        if self.follow_var.get() and self.table.get_children():
            self._select_event(self._latest_display_item())

    def _poll(self) -> None:
        if not self.winfo_exists():
            return
        try:
            latest_id, fresh = self.store.activity_since(self.last_id)
            for event in fresh:
                self._add_event(event)
            self.last_id = latest_id
            if fresh and self.follow_var.get():
                latest = self._latest_display_item()
                if latest:
                    self._select_event(latest)
        except sqlite3.Error as exc:
            self.detail_var.set(f"Activity log unavailable: {exc}")
            self.detail_label.pack(fill="x", side="bottom", before=self.content_split)
        self._poll_job = self.after(350, self._poll)

    def destroy(self) -> None:
        if self._selection_job:
            self.after_cancel(self._selection_job)
            self._selection_job = None
        if getattr(self, '_poll_job', None):
            self.after_cancel(self._poll_job)
            self._poll_job = None
        super().destroy()

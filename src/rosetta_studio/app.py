from __future__ import annotations

import os
import json
import shutil
import sys
import threading
import queue
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .activity_window import ActivityWindow
from .event_store import DEFAULT_DB
from .ui import BG, INK, MUTED, ACCENT, PANEL, LINE, CodePane, configure_style


def _configure_converter_paths() -> None:
    if os.name != "nt" or not os.environ.get("LOCALAPPDATA"):
        return
    settings = Path(os.environ["LOCALAPPDATA"]) / "Rosetta" / "Harness" / "converters.json"
    try:
        configured = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(configured, dict):
        return
    for name, variable in (
        ("t2l", "ROSETTA_TYLAX"),
        ("pandoc", "ROSETTA_PANDOC"),
        ("sv2v", "ROSETTA_SV2V"),
        ("typst", "ROSETTA_TYPST"),
        ("nu", "ROSETTA_NUSHELL"),
    ):
        path = configured.get(name)
        if isinstance(path, str) and Path(path).is_file():
            os.environ.setdefault(variable, path)


_configure_converter_paths()

from .core import PAIR_LABELS, available_pairs, experimental_pairs, result_text


ROOT = Path(__file__).resolve().parent
EXAMPLES = ROOT / "examples"


def _configure_tk_runtime() -> None:
    """Locate bundled Tcl/Tk when a Windows virtualenv misses its base install."""
    if os.name != "nt":
        return
    tcl_root = Path(sys.base_prefix) / "tcl"
    for variable, folder, sentinel in (
        ("TCL_LIBRARY", "tcl8.*", "init.tcl"),
        ("TK_LIBRARY", "tk8.*", "tk.tcl"),
    ):
        if variable in os.environ:
            continue
        for candidate in sorted(tcl_root.glob(folder), reverse=True):
            if (candidate / sentinel).is_file():
                os.environ[variable] = str(candidate)
                break


def _configured_workspace_path() -> Path | None:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return None
    settings = Path(local_app_data) / "Rosetta" / "Harness" / "workspace.json"
    try:
        configured = json.loads(settings.read_text(encoding="utf-8"))
        workspace = Path(configured["workspace"]).resolve()
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return workspace if workspace.is_dir() else None


def _save_workspace_path(workspace: Path) -> None:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return
    settings = Path(local_app_data) / "Rosetta" / "Harness" / "workspace.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps({"workspace": str(workspace)}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


class RosettaStudio(tk.Tk):
    def __init__(self) -> None:
        _configure_tk_runtime()
        super().__init__()
        self._closing = False
        self._poll_job: str | None = None
        self._example_job: str | None = None
        self._activity_job: str | None = None
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.title("Rosetta Studio")
        self.geometry("1280x790")
        self.minsize(980, 660)
        self.configure(bg=BG)
        self.pairs = available_pairs()
        self.experimental = experimental_pairs()
        self.example_items: dict[str, str] = {}
        self.example_var = tk.StringVar()
        self.example_folder: Path | None = None
        self.task_details = ""
        self.target_path: Path | None = None
        self.workspace_path: Path | None = _configured_workspace_path()
        self.agent_path: Path | None = None
        self.activity_window: ActivityWindow | None = None
        self.busy = False
        self.events: queue.Queue[tuple[str, str, str, bool | None]] = queue.Queue()
        self._build()
        self._select_pair()
        self._poll_job = self.after(40, self._poll_events)
        self._example_job = self.after(150, self._load_default_example)

    def _load_default_example(self) -> None:
        self._example_job = None
        if not self._closing:
            self.load_example("typst")

    def destroy(self) -> None:
        """Stop Tk callbacks and child windows before destroying the root."""
        if self._closing:
            return
        self._closing = True
        for attribute in ("_poll_job", "_example_job", "_activity_job"):
            job = getattr(self, attribute, None)
            if job is not None:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
                setattr(self, attribute, None)
        child = getattr(self, "activity_window", None)
        if child is not None:
            try:
                if child.winfo_exists():
                    child.destroy()
            except tk.TclError:
                pass
            self.activity_window = None
        try:
            self.update_idletasks()
        except tk.TclError:
            pass
        super().destroy()

    def _build(self) -> None:
        configure_style(self)

        top = tk.Frame(self, bg=BG, padx=24, pady=17)
        top.pack(fill="x")
        tk.Label(top, text="Rosetta", bg=BG, fg=INK,
                 font=("Segoe UI", 20, "bold")).pack(side="left")
        tk.Label(top, text="STUDIO", bg=BG, fg=MUTED,
                 font=("Segoe UI", 10)).pack(side="left", padx=(14, 0))
        self.pair_var = tk.StringVar(value=PAIR_LABELS[0])
        self.pair_box = ttk.Combobox(top, textvariable=self.pair_var,
                                     values=PAIR_LABELS, state="readonly", width=31)
        self.pair_box.pack(side="right")
        self.pair_box.bind("<<ComboboxSelected>>", lambda _e: self._select_pair())

        bar = tk.Frame(self, bg=PANEL, highlightbackground=LINE, highlightthickness=1,
                       padx=14, pady=10)
        bar.pack(fill="x", padx=24)
        ttk.Button(bar, text="Open target", command=self.open_target).pack(side="left")
        ttk.Button(bar, text="Save target", command=self.save_target).pack(
            side="left", padx=(8, 16))
        self.project_button = ttk.Button(bar, text="Project to agent view  →",
                                         style="Accent.TButton", command=self.project)
        self.project_button.pack(side="left")
        self.deliver_button = ttk.Button(bar, text="←  Translate to target view",
                                         command=self.deliver)
        self.deliver_button.pack(side="left", padx=(8, 0))
        self.backend_var = tk.StringVar()
        tk.Label(bar, textvariable=self.backend_var, bg=PANEL, fg=MUTED,
                 font=("Segoe UI", 9)).pack(side="right", padx=(8, 0))

        examples_bar = tk.Frame(self, bg=BG, padx=24, pady=8)
        examples_bar.pack(fill="x")
        tk.Label(examples_bar, text="EXAMPLES", bg=BG, fg=MUTED,
                 font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 12))
        self.example_box = ttk.Combobox(examples_bar, textvariable=self.example_var,
                                         state="readonly", width=64)
        self.example_box.pack(side="left", fill="x", expand=True)
        ttk.Button(examples_bar, text="Load", command=self.load_selected_example).pack(
            side="left", padx=(8, 0))
        ttk.Button(examples_bar, text="Task details", command=self.show_task_details).pack(
            side="left", padx=(8, 0))
        ttk.Button(examples_bar, text="Prepare workspace", command=self.prepare_workspace).pack(
            side="left", padx=(8, 0))

        handoff = tk.Frame(self, bg=BG, padx=24, pady=4)
        handoff.pack(fill="x")
        tk.Label(handoff, text="AGENT HANDOFF", bg=BG, fg=MUTED,
                 font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 12))
        ttk.Button(handoff, text="Save agent file", command=self.save_agent_view).pack(side="left")
        ttk.Button(handoff, text="Reload agent file", command=self.open_agent_view).pack(
            side="left", padx=(8, 0))
        ttk.Button(handoff, text="Copy instructions", command=self.copy_agent_instructions).pack(
            side="left", padx=(8, 0))
        ttk.Button(handoff, text="Agent activity", command=self.show_agent_activity).pack(
            side="right")
        ttk.Button(handoff, text="Select workspace", command=self.select_workspace).pack(
            side="right", padx=(0, 8))

        task = tk.Frame(self, bg=BG, padx=24, pady=10)
        task.pack(fill="x")
        tk.Label(task, text="TASK", bg=BG, fg=MUTED,
                 font=("Segoe UI", 9, "bold")).pack(side="left", anchor="n", padx=(0, 14), pady=7)
        self.task_var = tk.StringVar(value="Open a target artifact or load an example.")
        task_label = tk.Label(task, textvariable=self.task_var, bg=BG, fg=INK,
                 font=("Segoe UI", 10), anchor="w", justify="left",
                 wraplength=1020)
        task_label.pack(side="left", fill="x", expand=True, pady=5)
        task.bind('<Configure>', lambda event: task_label.configure(wraplength=max(100, event.width - 130)))

        panes = tk.Frame(self, bg=BG, padx=24, pady=16)
        panes.pack(fill="both", expand=True)
        panes.grid_columnconfigure(0, weight=1, uniform="pane")
        panes.grid_columnconfigure(1, weight=1, uniform="pane")
        panes.grid_rowconfigure(0, weight=1)
        self.target_text = self._editor(panes, 0, "TARGET ARTIFACT", "Human-facing format")
        self.view_text = self._editor(panes, 1, "AGENT VIEW", "Familiar working format")

        foot = tk.Frame(self, bg=BG, padx=24, pady=16)
        foot.pack(fill="x", side="bottom", before=panes)
        foot.columnconfigure(0, weight=1, uniform="status")
        foot.columnconfigure(1, weight=1, uniform="status")
        self.status_var = tk.StringVar(value="Ready")
        self.status = tk.Label(foot, textvariable=self.status_var, bg=BG, fg=ACCENT,
                               font=("Segoe UI", 9, "bold"), anchor="w")
        self.status.grid(row=0, column=0, sticky="w")
        self.notes_var = tk.StringVar(value="")
        notes = tk.Label(foot, textvariable=self.notes_var, bg=BG, fg=MUTED,
                        font=("Segoe UI", 9), anchor="e", justify="right")
        notes.grid(row=0, column=1, sticky="e")
        def resize_status(event: tk.Event) -> None:
            width = max(100, (event.width - 48) // 2)
            self.status.configure(wraplength=width)
            notes.configure(wraplength=width)
        foot.bind('<Configure>', resize_status)

    def show_agent_activity(self) -> None:
        db = self._activity_db()
        if self.activity_window is None or not self.activity_window.winfo_exists():
            self.activity_window = ActivityWindow(self, events_db=db)
        else:
            if self.activity_window.store.path != db.resolve():
                self.activity_window.destroy()
                self.activity_window = ActivityWindow(self, events_db=db)
            else:
                self.activity_window.lift()
                self.activity_window.focus_force()

    def select_workspace(self) -> None:
        chosen = filedialog.askdirectory(title="Select the DeepSeek Harness workspace")
        if chosen:
            self.workspace_path = Path(chosen).resolve()
            try:
                _save_workspace_path(self.workspace_path)
            except OSError as exc:
                self.status_var.set(f"Workspace selected for this run; could not save it: {exc}")
                if self.activity_window is not None and self.activity_window.winfo_exists():
                    self.show_agent_activity()
                return
            self.status_var.set(f"Activity workspace: {self.workspace_path}")
            if self.activity_window is not None and self.activity_window.winfo_exists():
                self.show_agent_activity()

    def _activity_db(self) -> Path:
        if self.workspace_path is not None:
            return self.workspace_path / ".rosetta" / "events.sqlite3"
        if self.target_path is not None:
            for folder in (self.target_path.parent, *self.target_path.parents):
                candidate = folder / ".rosetta" / "events.sqlite3"
                if candidate.is_file():
                    return candidate
        current_workspace_db = Path.cwd() / ".rosetta" / "events.sqlite3"
        if current_workspace_db.is_file():
            return current_workspace_db
        return DEFAULT_DB

    def _editor(self, parent: tk.Widget, column: int, title: str, subtitle: str) -> tk.Text:
        frame = tk.Frame(parent, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        frame.grid(row=0, column=column, sticky="nsew", padx=(0, 8) if column == 0 else (8, 0))
        head = tk.Frame(frame, bg=PANEL, padx=14, pady=11)
        head.pack(fill="x")
        tk.Label(head, text=title, bg=PANEL, fg=INK,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w")
        label = tk.Label(head, text=subtitle, bg=PANEL, fg=MUTED,
                         font=("Segoe UI", 9))
        label.pack(anchor="w", pady=(3, 0))
        if column == 0:
            self.target_format = label
        else:
            self.view_format = label
        pane = CodePane(frame)
        pane.pack(fill="both", expand=True)
        return pane.text

    def _select_pair(self) -> None:
        label = self.pair_var.get()
        self._refresh_examples()
        pair = self.pairs.get(label) or self.experimental.get(label)
        enabled = pair is not None
        if enabled:
            self.target_format.configure(text=f"Target file  ·  {pair.target_extension}")
            self.view_format.configure(text=f"Agent view  ·  {pair.view_extension}")
            self.backend_var.set("EXPERIMENTAL BACKEND" if pair.experimental
                                 else "DETERMINISTIC BACKEND")
            if pair.experimental:
                self.status_var.set("Experimental converter: supported syntax only; inspect its output.")
                self.notes_var.set("Supported syntax subset only; inspect the delivered output.")
            else:
                self.status_var.set("Ready")
                self.notes_var.set("")
        else:
            self.target_format.configure(text="Target file")
            self.view_format.configure(text="Agent view")
            self.backend_var.set("NO ADAPTER")
            self.status_var.set("No converter is available for this pair.")
            self.notes_var.set("")
        state = "normal" if enabled else "disabled"
        self.project_button.configure(state=state)
        self.deliver_button.configure(state=state)

    def _refresh_examples(self) -> None:
        demo = {
            "LaTeX / Typst": "typst",
            "Verilog / SystemVerilog": "systemverilog",
            "PRQL / SQL": "prql",
            "Bash / Nushell (experimental)": "nushell",
        }[self.pair_var.get()]
        items = {"Demo | small example": demo}
        previous = self.example_var.get()
        self.example_items = items
        self.example_box.configure(values=list(items))
        self.example_var.set(previous if previous in items else next(iter(items)))

    def load_selected_example(self) -> None:
        self.load_example(self.example_items[self.example_var.get()])

    def show_task_details(self) -> None:
        if not self.task_details:
            messagebox.showinfo("Task details", "Load an example first.")
            return
        window = tk.Toplevel(self)
        window.title("Task details")
        window.geometry("760x560")
        editor = tk.Text(window, wrap="word", padx=18, pady=16,
                         font=("Segoe UI", 10), bg=PANEL, fg=INK)
        scroll = ttk.Scrollbar(window, orient="vertical", command=editor.yview)
        editor.configure(yscrollcommand=scroll.set)
        editor.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        editor.insert("1.0", self.task_details)
        editor.configure(state="disabled")

    def prepare_workspace(self) -> None:
        if self.example_folder is None:
            messagebox.showinfo("Prepare workspace", "Load an example first.")
            return
        parent = filedialog.askdirectory(title="Choose a workspace directory",
                                         initialdir=Path.cwd())
        if not parent:
            return
        parent_path = Path(parent).resolve()
        destination = parent_path / self.example_folder.name
        if destination.exists():
            messagebox.showerror("Prepare workspace", f"Already exists: {destination}")
            return
        shutil.copytree(self.example_folder, destination)
        target_name = next(p.name for p in self.example_folder.glob("target.*"))
        self.target_path = destination / target_name
        self.target_path.write_text(self.target_text.get("1.0", "end-1c"), encoding="utf-8")
        self.status_var.set(f"Workspace ready: {self.target_path}")
        self.notes_var.set("Target and task data copied; agent can use this target path via MCP.")

    def _load_current_example(self) -> None:
        mapping = {
            "LaTeX / Typst": "typst",
            "Verilog / SystemVerilog": "systemverilog",
            "PRQL / SQL": "prql",
            "Bash / Nushell (experimental)": "nushell",
        }
        self.load_example(mapping[self.pair_var.get()])

    def load_example(self, name: str) -> None:
        folder = EXAMPLES / name
        suffix = {
            "typst": ".typ",
            "systemverilog": ".sv",
            "prql": ".prql",
            "nushell": ".nu",
        }[name]
        target = folder / f"target{suffix}"
        if not target.exists():
            return
        self.target_text.delete("1.0", "end")
        self.target_text.insert("1.0", target.read_text(encoding="utf-8"))
        task_note = folder / "TASK.md"
        self.task_var.set(task_note.read_text(encoding="utf-8").strip() if task_note.exists() else "")
        self.view_text.delete("1.0", "end")
        self.agent_path = None
        self.target_path = None
        self.example_folder = folder
        self.task_details = self.task_var.get()
        if name == "typst":
            self.pair_var.set("LaTeX / Typst")
        elif name == "systemverilog":
            self.pair_var.set("Verilog / SystemVerilog")
        elif name == "prql":
            self.pair_var.set("PRQL / SQL")
        else:
            self.pair_var.set("Bash / Nushell (experimental)")
        self._select_pair()
        if name in {"typst", "systemverilog", "prql"}:
            self.status_var.set(f"Loaded example: {name}")

    def open_target(self) -> None:
        pair = self.pairs.get(self.pair_var.get()) or self.experimental.get(self.pair_var.get())
        if not pair:
            return
        path = filedialog.askopenfilename(
            filetypes=[(f"Target (*{pair.target_extension})", f"*{pair.target_extension}"),
                       ("All files", "*.*")])
        if path:
            self.example_folder = None
            self.task_details = ""
            self.target_path = Path(path)
            self.agent_path = None
            self.target_text.delete("1.0", "end")
            self.target_text.insert("1.0", self.target_path.read_text(encoding="utf-8"))
            self.view_text.delete("1.0", "end")
            self.status_var.set(f"Opened {self.target_path.name}")

    def save_target(self) -> None:
        pair = self.pairs.get(self.pair_var.get()) or self.experimental.get(self.pair_var.get())
        if not pair:
            return
        path = self.target_path
        if path is None:
            chosen = filedialog.asksaveasfilename(
                defaultextension=pair.target_extension,
                filetypes=[(f"Target (*{pair.target_extension})", f"*{pair.target_extension}"),
                           ("All files", "*.*")])
            if not chosen:
                return
            path = Path(chosen)
            self.target_path = path
        path.write_text(self.target_text.get("1.0", "end-1c"), encoding="utf-8")
        self.status_var.set(f"Saved {path.name}")

    def save_agent_view(self) -> None:
        pair = self.pairs.get(self.pair_var.get()) or self.experimental.get(self.pair_var.get())
        if not pair:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=pair.view_extension,
            initialfile=f"agent_view{pair.view_extension}",
            filetypes=[(f"Agent view (*{pair.view_extension})", f"*{pair.view_extension}"),
                       ("All files", "*.*")])
        if path:
            self.agent_path = Path(path)
            self.agent_path.write_text(self.view_text.get("1.0", "end-1c"), encoding="utf-8")
            self.status_var.set(f"Saved agent file: {self.agent_path.name}")
            self.notes_var.set("Give this file and the task instructions to your coding agent.")

    def open_agent_view(self) -> None:
        pair = self.pairs.get(self.pair_var.get()) or self.experimental.get(self.pair_var.get())
        if not pair:
            return
        path = filedialog.askopenfilename(
            filetypes=[(f"Agent view (*{pair.view_extension})", f"*{pair.view_extension}"),
                       ("All files", "*.*")])
        if path:
            self.agent_path = Path(path)
            self.view_text.delete("1.0", "end")
            self.view_text.insert("1.0", self.agent_path.read_text(encoding="utf-8"))
            self.status_var.set(f"Loaded agent file: {self.agent_path.name}")

    def copy_agent_instructions(self) -> None:
        if self.agent_path is None:
            messagebox.showinfo("Agent handoff", "Save the agent file first.")
            return
        pair = self.pairs.get(self.pair_var.get()) or self.experimental.get(self.pair_var.get())
        task = self.task_var.get().strip()
        instructions = (
            f"Task:\n{task}\n\n"
            f"Edit this agent-facing file and save your changes: {self.agent_path.resolve()}\n"
            f"Working directory: {self.agent_path.resolve().parent}\n"
            f"Do not edit the target-format artifact directly. Rosetta Studio will translate "
            f"your saved changes back to {pair.target_extension} for review. "
            f"Stay within the supported syntax; report any construct you cannot represent."
        )
        if pair.experimental:
            instructions += " This Bash/Nushell converter is experimental; review the translated output carefully."
        self.clipboard_clear()
        self.clipboard_append(instructions)
        self.status_var.set("Agent instructions copied to clipboard.")
        self.notes_var.set(str(self.agent_path.resolve()))

    def _run_translation(self, direction: str) -> None:
        pair = self.pairs.get(self.pair_var.get()) or self.experimental.get(self.pair_var.get())
        if not pair or self.busy:
            return
        if direction == "project" and not self.target_text.get("1.0", "end-1c").strip():
            self.view_text.delete("1.0", "end")
            self.status_var.set("Blank target: write the new artifact in the agent view.")
            self.notes_var.set("Edit the familiar-format view, translate it back, then save the target file.")
            return
        self.busy = True
        self.project_button.configure(state="disabled")
        self.deliver_button.configure(state="disabled")
        self.status_var.set("Translating…")
        text = (self.target_text if direction == "project" else self.view_text).get("1.0", "end-1c")

        def work() -> None:
            try:
                result = pair.project(text) if direction == "project" else pair.deliver(text)
                content, note = result_text(result)
                error = None
            except Exception as exc:  # adapter errors are shown, not swallowed
                content, note, error = "", str(exc), True
            self.events.put((direction, content, note, error))

        threading.Thread(target=work, daemon=True).start()

    def _poll_events(self) -> None:
        self._poll_job = None
        try:
            while True:
                self._finish(*self.events.get_nowait())
        except queue.Empty:
            pass
        if not self._closing and self.winfo_exists():
            self._poll_job = self.after(40, self._poll_events)

    def _finish(self, direction: str, content: str, note: str, error: bool | None) -> None:
        self.busy = False
        self._select_pair()
        if error:
            self.status_var.set("Translation failed")
            self.notes_var.set(note[:180])
            messagebox.showerror("Rosetta conversion", note)
            return
        if not content:
            self.status_var.set("Translation failed")
            self.notes_var.set(note[:180])
            messagebox.showerror("Rosetta conversion", note)
            return
        destination = self.view_text if direction == "project" else self.target_text
        destination.delete("1.0", "end")
        destination.insert("1.0", content)
        self.status_var.set(
            "Projection complete" if direction == "project"
            else "Target view updated — click Save target to write the file"
        )
        self.notes_var.set(
            note[:180] if direction == "project"
            else "Translated output is shown on the left; it has not been saved to disk."
        )

    def project(self) -> None:
        self._run_translation("project")

    def deliver(self) -> None:
        self._run_translation("deliver")


def main() -> None:
    app = RosettaStudio()
    if "--activity" in sys.argv[1:]:
        app._activity_job = app.after(200, app.show_agent_activity)
    app.mainloop()


if __name__ == "__main__":
    main()

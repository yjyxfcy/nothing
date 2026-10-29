"""Presentation checks using isolated events, never the user's activity history."""
import gc
import json
import os
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from rosetta_studio.app import (
    RosettaStudio,
    _configure_converter_paths,
    _configure_tk_runtime,
    _configured_workspace_path,
    _save_workspace_path,
)
from rosetta_studio.activity_window import ActivityWindow
from rosetta_studio.event_store import EventStore
from rosetta_studio.ui import CodePane


class PresentationTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows installer settings")
    def test_studio_reads_local_converter_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "Rosetta" / "Harness" / "converters.json"
            settings.parent.mkdir(parents=True)
            converters = {
                "t2l": Path(tmp) / "t2l.exe",
                "pandoc": Path(tmp) / "pandoc.exe",
                "sv2v": Path(tmp) / "sv2v.exe",
                "typst": Path(tmp) / "typst.exe",
                "nu": Path(tmp) / "nu.exe",
            }
            for converter in converters.values():
                converter.touch()
            settings.write_text(
                json.dumps({name: str(path) for name, path in converters.items()}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"LOCALAPPDATA": tmp}, clear=True):
                _configure_converter_paths()
                self.assertEqual(os.environ.get("ROSETTA_TYLAX"), str(converters["t2l"]))
                self.assertEqual(os.environ.get("ROSETTA_PANDOC"), str(converters["pandoc"]))
                self.assertEqual(os.environ.get("ROSETTA_SV2V"), str(converters["sv2v"]))
                self.assertEqual(os.environ.get("ROSETTA_TYPST"), str(converters["typst"]))
                self.assertEqual(os.environ.get("ROSETTA_NUSHELL"), str(converters["nu"]))

    @unittest.skipUnless(os.name == "nt", "Windows installer settings")
    def test_studio_persists_and_reads_the_selected_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "project"
            workspace.mkdir()
            with patch.dict(os.environ, {"LOCALAPPDATA": str(root)}, clear=True):
                _save_workspace_path(workspace)
                self.assertEqual(_configured_workspace_path(), workspace.resolve())

    @classmethod
    def setUpClass(cls):
        _configure_tk_runtime()
        cls.root = tk.Tk()
        cls.root.withdraw()

    def test_studio_uses_current_workspace_activity(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            events = workspace / ".rosetta" / "events.sqlite3"
            events.parent.mkdir()
            events.touch()
            local_app_data = Path(tmp) / "local-app-data"
            with patch.dict(os.environ, {"LOCALAPPDATA": str(local_app_data)}):
                with patch("rosetta_studio.app.Path.cwd", return_value=workspace):
                    app = RosettaStudio()
                    try:
                        self.assertEqual(app._activity_db(), events)
                    finally:
                        app.destroy()

    def test_manual_delivery_label_does_not_claim_to_save_the_target(self):
        app = RosettaStudio()
        app.withdraw()
        try:
            self.assertEqual(app.deliver_button.cget("text"), "←  Translate to target view")
            app._finish("deliver", "target output", "backend note", None)
            self.assertIn("click Save target to write the file", app.status_var.get())
            self.assertIn("has not been saved to disk", app.notes_var.get())
        finally:
            app.destroy()

    def test_typst_example_instructions_use_plain_text_and_explain_saving(self):
        task = " ".join((
            Path(__file__).parents[1]
            / "src"
            / "rosetta_studio"
            / "examples"
            / "typst"
            / "TASK.md"
        ).read_text(encoding="utf-8").split())
        self.assertNotIn("**", task)
        self.assertIn("translate the result back to the Typst target view", task)
        self.assertIn("click the Save target button to write the file", task)

    @classmethod
    def tearDownClass(cls):
        # Collect orphaned Tk variables on their owning thread before later
        # asynchronous tests can trigger cyclic garbage collection elsewhere.
        gc.collect()
        cls.root.destroy()
        cls.root = None
        gc.collect()

    def test_code_annotations_do_not_change_content(self):
        pane = CodePane(self.root)
        source = '\\section{Runbook}\n% @TYPST-PASSTHROUGH\n% #set heading(numbering: "1.")\n% @END-TYPST-PASSTHROUGH\nText.'
        pane.text.insert('1.0', source)
        self.root.update()
        self.assertEqual(pane.text.get('1.0', 'end-1c'), source)
        self.assertEqual(pane.gutter.get('1.0', 'end-1c'), '1\n2\n3\n4\n5')
        self.assertIn('native', pane.text.tag_names('3.0'))
        self.assertNotIn('native', pane.text.tag_names('5.0'))
        self.assertIn('command', pane.text.tag_names('1.0'))
        pane.destroy()

    def test_session_filter_and_selection_keep_snapshots_paired(self):
        with tempfile.TemporaryDirectory() as scratch:
            db = Path(scratch) / 'events.sqlite3'
            store = EventStore(db)
            for session, action, status in [('alpha', 'commit', 'success'), ('beta', 'preview', 'error')]:
                store.record(source='Harness', session_id=session, pair_name='LaTeX / Typst',
                             action=action, status=status, target_path=str(Path(scratch) / 'run.typ'),
                             agent_view=session + ' agent', target_view=session + ' target', detail='Result ' + session)
            window = ActivityWindow(self.root, db)
            window.withdraw()
            self.root.update()
            self.assertEqual(window.target_text.get('1.0', 'end-1c'), 'beta target')
            self.assertEqual(window.status_label.cget('text'), 'ERROR')
            self.assertEqual(window.detail_label.winfo_manager(), 'pack')
            self.assertEqual(window.target_format_label.cget('text'), 'Typst')
            self.assertEqual(window.agent_format_label.cget('text'), 'LaTeX')
            latest = store.recent(1)[0]
            self.assertEqual(window.table.item(str(latest['id']), 'values')[0],
                             latest['created_at'][:19].replace('T', ' '))
            window.session_var.set('alpha')
            window._filter_sessions()
            self.root.update()
            self.assertEqual(len(window.table.get_children()), 1)
            self.assertEqual(window.agent_text.get('1.0', 'end-1c'), 'alpha agent')
            self.assertEqual(window.target_text.get('1.0', 'end-1c'), 'alpha target')
            self.assertEqual(window.detail_var.get(), 'Result alpha')
            self.assertFalse(window.detail_label.winfo_manager())
            self.assertEqual(window.stages['commit'].cget('foreground'), '#ffffff')
            self.assertEqual(window.target_text.cget('state'), 'disabled')
            window.session_var.set('All sessions')
            window._filter_sessions()
            self.assertEqual(len(window.table.get_children()), 2)
            window.deiconify()
            for width, height in [(980, 620), (1460, 790)]:
                window.geometry(f'{width}x{height}')
                self.root.update()
                bottom = window.detail_label.winfo_y() + window.detail_label.winfo_height()
                self.assertLessEqual(bottom, window.winfo_height())
                self.assertGreater(window.target_text.winfo_height(), 80)
                self.assertGreater(window.agent_text.winfo_height(), 80)
                columns_width = sum(window.table.column(col, 'width') for col in window.table['columns'])
                self.assertLessEqual(columns_width, window.table.winfo_width())
            window.destroy()

    def test_follow_latest_keeps_an_artifact_visible(self):
        with tempfile.TemporaryDirectory() as scratch:
            db = Path(scratch) / "events.sqlite3"
            store = EventStore(db)
            store.record(source="Harness", session_id="artifact", pair_name="LaTeX / Typst",
                         action="preview", status="success", target_path=str(Path(scratch) / "note.typ"),
                         agent_view="agent text", target_view="target text", detail="Preview")
            store.record(source="Harness", session_id=None, pair_name=None,
                         action="list_pairs", status="success", target_path=None,
                         agent_view=None, target_view=None, detail="Discover tools")
            for index in range(130):
                store.record(source="Harness", session_id=None, pair_name=None,
                             action="list_pairs", status="success", target_path=None,
                             agent_view=None, target_view=None, detail=f"Discover tools {index}")
            window = ActivityWindow(self.root, db)
            try:
                self.root.update()
                self.assertEqual(window.target_text.get("1.0", "end-1c"), "target text")
                self.assertEqual(window.agent_text.get("1.0", "end-1c"), "agent text")
                self.assertEqual(len(window.table.get_children()), 1)
                self.assertEqual(store.recent(1)[0]["action"], "list_pairs")

                store.record(source="Harness", session_id="artifact", pair_name="LaTeX / Typst",
                             action="commit", status="success", target_path=str(Path(scratch) / "note.typ"),
                             agent_view="new agent", target_view="new target", detail="Deliver")
                window._poll()
                self.root.update()
                self.assertEqual(window.target_text.get("1.0", "end-1c"), "new target")
                self.assertEqual(window.agent_text.get("1.0", "end-1c"), "new agent")
            finally:
                window.destroy()

    def test_activity_labels_match_target_and_agent_views(self):
        with tempfile.TemporaryDirectory() as scratch:
            db = Path(scratch) / 'events.sqlite3'
            store = EventStore(db)
            window = ActivityWindow(self.root, db)
            window.withdraw()
            try:
                for pair, target, agent in (
                    ('LaTeX / Typst', 'Typst', 'LaTeX'),
                    ('Verilog / SystemVerilog', 'SystemVerilog', 'Verilog'),
                    ('PRQL / SQL', 'PRQL', 'SQL'),
                    ('Bash / Nushell (experimental)', 'Nushell', 'Bash'),
                ):
                    with self.subTest(pair=pair):
                        store.record(source='Harness', session_id='label-check', pair_name=pair,
                                     action='open', status='success', target_path='demo',
                                     agent_view='agent', target_view='target', detail='')
                        window._show_event(store.recent(1)[0]['id'])
                        self.assertEqual(window.target_format_label.cget('text'), target)
                        self.assertEqual(window.agent_format_label.cget('text'), agent)
            finally:
                window.destroy()

    def test_help_boxes_are_toggleable_and_read_only(self):
        with tempfile.TemporaryDirectory() as scratch:
            db = Path(scratch) / 'events.sqlite3'
            source = ('\\section{Runbook}\n% @TYPST-PASSTHROUGH\n'
                      '% #show heading: it => text(fill: red, it.body)\n'
                      '% @END-TYPST-PASSTHROUGH\nText.')
            EventStore(db).record(
                source='Harness', session_id='help-test', pair_name='LaTeX / Typst',
                action='preview', status='success', target_path='run.typ',
                agent_view=source, target_view=source, detail='Preview only')
            window = ActivityWindow(self.root, db)
            self.root.update()
            window._toggle_help()
            self.root.update()
            self.assertEqual(window.help_button.cget('text'), 'Hide help')
            self.assertFalse(window.follow_var.get())
            self.assertIn('Old tool', window.agent_title_label.cget('text'))
            for pane, label in zip(window.view_panes, window.help_labels):
                self.assertTrue(label.winfo_ismapped())
                self.assertEqual(len(pane.help_borders), 4)
                self.assertEqual(pane.text.get('1.0', 'end-1c'), source)
                self.assertEqual(pane.text.cget('state'), 'disabled')
            for width, height in [(980, 620), (1460, 790)]:
                window.geometry(f'{width}x{height}')
                self.root.update()
                for pane in window.view_panes:
                    self.assertGreater(pane.text.winfo_height(), 80)
            window._toggle_help()
            self.root.update()
            for pane, label in zip(window.view_panes, window.help_labels):
                self.assertFalse(label.winfo_ismapped())
                self.assertFalse(pane.help_borders)
                self.assertEqual(pane.text.get('1.0', 'end-1c'), source)
            window.destroy()


if __name__ == '__main__':
    unittest.main()

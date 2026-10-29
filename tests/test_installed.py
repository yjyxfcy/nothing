"""Run with an installed wheel from a directory outside the source tree."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import tempfile
import unittest
from importlib.metadata import files as distribution_files
from importlib.resources import files
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ToolError

from compat_layer.interface import TranslationResult
from rosetta_studio.core import Pair, available_pairs, experimental_pairs
from rosetta_studio.mcp_server import RosettaMCP, preserved_regions, retains_regions


def unpack(result):
    assert not result.isError, result
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured.get("result", structured)
    return json.loads(result.content[0].text)


class InstalledPackageTests(unittest.TestCase):
    def test_pair_marks_target_only_content_before_projecting(self):
        def project(content):
            return TranslationResult.ok(content)

        pair = Pair("Test", ".new", ".old", project, project,
                    mark_unrepresentable=lambda content: ("protected:" + content, ["one region"]))
        result = pair.project("target-only")
        self.assertEqual(result.content, "protected:target-only")
        self.assertEqual(result.metadata["preserved_regions"], ["one region"])

    def test_typst_target_only_directive_survives_round_trip(self):
        pair = available_pairs()["LaTeX / Typst"]
        source = "= Demo\n\n#show heading: set text(fill: blue)\n"
        projected = pair.project(source)
        self.assertTrue(projected.success, projected.errors)
        self.assertIn("@TYPST-PASSTHROUGH", projected.content)
        delivered = pair.deliver(projected.content)
        self.assertTrue(delivered.success, delivered.errors)
        self.assertIn("#show heading: set text(fill: blue)", delivered.content)

    def test_mcp_refuses_projection_that_drops_marked_region(self):
        with tempfile.TemporaryDirectory() as temp:
            app = RosettaMCP(Path(temp), Path(temp) / "events.sqlite3")
            app.pairs["Test"] = Pair(
                "Test", ".new", ".old",
                lambda content: TranslationResult.ok("translated, but no marker"),
                lambda content: TranslationResult.ok(content),
                mark_unrepresentable=lambda content: (
                    "// @TYPST-PASSTHROUGH\n" + content + "\n// @END-TYPST-PASSTHROUGH",
                    ["target-only region"],
                ),
            )
            with self.assertRaisesRegex(ToolError, "dropped a target-only region"):
                app._start_session("Test", Path(temp) / "document.new", "target-only", None, False)

            app.pairs["Test"] = Pair(
                "Test", ".new", ".old",
                lambda content: TranslationResult.ok("translated, but no marker"),
                lambda content: TranslationResult.ok(content),
            )
            existing = "// @TYPST-PASSTHROUGH\ntarget-only\n// @END-TYPST-PASSTHROUGH"
            with self.assertRaisesRegex(ToolError, "dropped a target-only region"):
                app._start_session("Test", Path(temp) / "document.new", existing, None, False)

            app.pairs["Test"] = Pair(
                "Test", ".new", ".old",
                lambda content: TranslationResult.ok(""),
                lambda content: TranslationResult.ok(content),
            )
            with self.assertRaisesRegex(ToolError, "empty agent view"):
                app._start_session("Test", Path(temp) / "document.new", "nonempty", None, False)

            app.pairs["Test"] = Pair(
                "Test", ".new", ".old",
                lambda content: TranslationResult.ok("agent view"),
                lambda content: TranslationResult.ok(""),
            )
            session_id = app._start_session("Test", Path(temp) / "document.new",
                                            "nonempty", None, False)["session_id"]
            with self.assertRaisesRegex(ToolError, "empty target"):
                asyncio.run(app.server.call_tool("rosetta_preview_delivery", {"session_id": session_id}))

    def test_passthrough_comparison_keeps_line_endings_and_allows_new_blocks(self):
        original = "# @NU-PASSTHROUGH\n# print one\n# @END-NU-PASSTHROUGH\n"
        required = preserved_regions(original)
        self.assertTrue(retains_regions(original + original, required))
        self.assertFalse(retains_regions(original.replace("\n", "\r\n"), required))
        with self.assertRaisesRegex(ToolError, "Unterminated"):
            preserved_regions("# @NU-PASSTHROUGH\n# print one\n")

    def test_mcp_empty_preview_is_rejected_before_conversion_or_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = RosettaMCP(root, root / "events.sqlite3")

            def unexpected_delivery(content):
                self.fail("An empty view must not reach the conversion backend.")

            app.pairs["Test"] = Pair(
                "Test", ".new", ".old",
                lambda content: TranslationResult.ok(content), unexpected_delivery,
            )
            empty = app._start_session("Test", root / "new.new", "", None, False)
            with self.assertRaisesRegex(ToolError, "Agent view is empty"):
                asyncio.run(app.server.call_tool("rosetta_preview_delivery", {
                    "session_id": empty["session_id"],
                }))
            self.assertFalse((root / "new.new").exists())

            target = root / "existing.new"
            original = b"original content\n"
            target.write_bytes(original)
            opened = app._start_session("Test", target, original.decode("utf-8"),
                                        hashlib.sha256(original).hexdigest(), True)
            session = app.sessions[opened["session_id"]]
            for content in ("", " \r\n\t"):
                with self.subTest(preview=repr(content)):
                    session.preview_view = content
                    with self.assertRaisesRegex(ToolError, "refusing to clear the target"):
                        asyncio.run(app.server.call_tool("rosetta_commit_delivery", {
                            "session_id": opened["session_id"],
                        }))
                    self.assertEqual(target.read_bytes(), original)

    def test_mcp_blank_edits_preserve_content_and_new_sessions_can_recover(self):
        async def check():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                target = root / "existing.nu"
                original = (
                    'print (open --raw "data/access.log" | lines | '
                    'where {|l| ($l =~ "GET") } | length)\n'
                ).encode("utf-8")
                target.write_bytes(original)
                params = StdioServerParameters(
                    command=sys.executable,
                    args=["-m", "rosetta_studio.mcp_server", "--workspace", temp,
                          "--events-db", str(root / "events.sqlite3")],
                    cwd=temp,
                )
                async with stdio_client(params) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as client:
                        await client.initialize()
                        opened = unpack(await client.call_tool("rosetta_open_artifact", {
                            "pair_name": "Bash / Nushell (experimental)",
                            "target_path": "existing.nu",
                        }))
                        sid = opened["session_id"]
                        unpack(await client.call_tool("rosetta_preview_delivery", {"session_id": sid}))
                        for content in ("", " \r\n\t"):
                            rejected = await client.call_tool("rosetta_write_agent_view", {
                                "session_id": sid, "content": content,
                            })
                            self.assertTrue(rejected.isError)
                            self.assertIn("previous view is unchanged", rejected.content[0].text)
                            self.assertEqual(unpack(await client.call_tool("rosetta_read_agent_view", {
                                "session_id": sid,
                            })), opened["agent_view"])
                            self.assertEqual(target.read_bytes(), original)
                        committed = unpack(await client.call_tool("rosetta_commit_delivery", {
                            "session_id": sid,
                        }))
                        self.assertTrue(committed["written"])
                        self.assertTrue(target.read_bytes())

                        created = unpack(await client.call_tool("rosetta_create_artifact", {
                            "pair_name": "Bash / Nushell (experimental)",
                            "target_path": "created.nu", "initial_target_content": "",
                        }))
                        new_sid = created["session_id"]
                        rejected = await client.call_tool("rosetta_preview_delivery", {
                            "session_id": new_sid,
                        })
                        self.assertTrue(rejected.isError)
                        self.assertIn("Agent view is empty", rejected.content[0].text)
                        self.assertFalse((root / "created.nu").exists())
                        unpack(await client.call_tool("rosetta_write_agent_view", {
                            "session_id": new_sid, "content": opened["agent_view"],
                        }))
                        unpack(await client.call_tool("rosetta_preview_delivery", {"session_id": new_sid}))
                        unpack(await client.call_tool("rosetta_commit_delivery", {"session_id": new_sid}))
                        self.assertTrue((root / "created.nu").read_bytes())
        asyncio.run(check())

    def test_mcp_typst_passthrough_cycle(self):
        async def check():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                target = root / "document.typ"
                target.write_text("= Demo\n\n#show heading: set text(fill: blue)\n", encoding="utf-8")
                params = StdioServerParameters(
                    command=sys.executable,
                    args=["-m", "rosetta_studio.mcp_server", "--workspace", temp,
                          "--events-db", str(root / "events.sqlite3")],
                    cwd=temp,
                )
                async with stdio_client(params) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as client:
                        await client.initialize()
                        opened = unpack(await client.call_tool("rosetta_open_artifact", {
                            "pair_name": "LaTeX / Typst", "target_path": "document.typ",
                        }))
                        self.assertIn("@TYPST-PASSTHROUGH", opened["agent_view"])
                        sid = opened["session_id"]
                        preview = unpack(await client.call_tool("rosetta_preview_delivery", {
                            "session_id": sid,
                        }))
                        self.assertIn("#show heading: set text(fill: blue)", preview["target_content"])
                        committed = unpack(await client.call_tool("rosetta_commit_delivery", {
                            "session_id": sid,
                        }))
                        self.assertTrue(committed["written"])
                        self.assertIn("#show heading: set text(fill: blue)",
                                      target.read_text(encoding="utf-8"))
        asyncio.run(check())

    def test_distribution_excludes_evaluation_code(self):
        names = [str(item).replace("\\", "/") for item in distribution_files("rosetta-compat-layer")]
        self.assertFalse(any(part in name for name in names for part in (
            "experiments/", "benchmark/", "evaluate.py", "judge.py", "run_agent.py",
        )))

    def test_gui_starts_without_paper_catalog(self):
        from rosetta_studio.app import RosettaStudio

        app = RosettaStudio()
        try:
            app.update()
            self.assertEqual(app.example_items, {"Demo | small example": "typst"})
        finally:
            app.destroy()
        self.assertTrue(app._closing)
        self.assertIsNone(app._poll_job)
        self.assertIsNone(app._example_job)
        self.assertIsNone(app._activity_job)

    def test_four_pairs(self):
        pairs = available_pairs() | experimental_pairs()
        self.assertEqual(len(pairs), 4)
        examples = files("rosetta_studio").joinpath("examples")
        for pair_name, folder, suffix in (
            ("LaTeX / Typst", "typst", "typ"),
            ("Verilog / SystemVerilog", "systemverilog", "sv"),
            ("PRQL / SQL", "prql", "prql"),
            ("Bash / Nushell (experimental)", "nushell", "nu"),
        ):
            with self.subTest(pair=pair_name):
                target = examples.joinpath(folder, f"target.{suffix}").read_text(encoding="utf-8")
                projected = pairs[pair_name].project(target)
                self.assertTrue(projected.success, projected.errors)
                delivered = pairs[pair_name].deliver(projected.content)
                self.assertTrue(delivered.success, delivered.errors)
                self.assertTrue(delivered.content)

    def test_unsafe_shell_assumptions_refused(self):
        pair = experimental_pairs()["Bash / Nushell (experimental)"]
        for bash in (
            'if [[ ! -f "data.txt" ]]; then echo missing >&2; exit 1; fi\ncat data.txt | wc -l',
            'LC_ALL=C sort data.txt',
            'cat data.txt | uniq',
        ):
            with self.subTest(bash=bash):
                self.assertFalse(pair.deliver(bash).success)

    def test_interface_only_sv_refuses_empty_projection(self):
        source = (
            "interface bus_if(input logic clk); logic d; "
            "modport dut(input clk, output d); endinterface\n"
            "module TopModule(bus_if.dut b); assign b.d = b.clk; endmodule"
        )
        pair = available_pairs()["Verilog / SystemVerilog"]
        result = pair.project(source)
        self.assertFalse(result.success)
        self.assertIn("empty projection", " ".join(result.errors))

    def test_mcp_write_cycle(self):
        async def check():
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                target = root / "sample.nu"
                sample = files("rosetta_studio").joinpath("examples", "nushell", "target.nu")
                target.write_text(sample.read_text(encoding="utf-8"), encoding="utf-8")
                params = StdioServerParameters(
                    command=sys.executable,
                    args=["-m", "rosetta_studio.mcp_server", "--workspace", temp,
                          "--events-db", str(root / "events.sqlite3")],
                    cwd=temp,
                )
                async with stdio_client(params) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as client:
                        await client.initialize()
                        opened = unpack(await client.call_tool("rosetta_open_artifact", {
                            "pair_name": "Bash / Nushell (experimental)",
                            "target_path": "sample.nu",
                        }))
                        sid = opened["session_id"]
                        self.assertIn("grep GET", opened["agent_view"])
                        edited = opened["agent_view"].replace("grep GET", "grep POST", 1)
                        protected = opened["agent_view"].split("# @NU-PASSTHROUGH", 1)[1]
                        self.assertIn("group-by", protected)
                        damaged = await client.call_tool("rosetta_write_agent_view", {
                            "session_id": sid,
                            "content": opened["agent_view"].replace("group-by method", "group-by status"),
                        })
                        self.assertTrue(damaged.isError)
                        no_preview = await client.call_tool("rosetta_commit_delivery", {
                            "session_id": sid,
                        })
                        self.assertTrue(no_preview.isError)
                        unpack(await client.call_tool("rosetta_write_agent_view", {
                            "session_id": sid, "content": edited,
                        }))
                        preview = unpack(await client.call_tool("rosetta_preview_delivery", {
                            "session_id": sid,
                        }))
                        self.assertIn("POST", preview["target_content"])
                        unpack(await client.call_tool("rosetta_write_agent_view", {
                            "session_id": sid, "content": edited.replace("grep POST", "grep PUT", 1),
                        }))
                        stale_preview = await client.call_tool("rosetta_commit_delivery", {
                            "session_id": sid,
                        })
                        self.assertTrue(stale_preview.isError)
                        unpack(await client.call_tool("rosetta_write_agent_view", {
                            "session_id": sid, "content": edited,
                        }))
                        unpack(await client.call_tool("rosetta_preview_delivery", {
                            "session_id": sid,
                        }))
                        committed = unpack(await client.call_tool("rosetta_commit_delivery", {
                            "session_id": sid,
                        }))
                        self.assertTrue(committed["written"])
                        self.assertIn("POST", target.read_text(encoding="utf-8"))
                        self.assertIn("group-by method", target.read_text(encoding="utf-8"))
                        stale = unpack(await client.call_tool("rosetta_open_artifact", {
                            "pair_name": "Bash / Nushell (experimental)",
                            "target_path": "sample.nu",
                        }))
                        unpack(await client.call_tool("rosetta_preview_delivery", {
                            "session_id": stale["session_id"],
                        }))
                        target.write_text("# edited outside Rosetta\n", encoding="utf-8")
                        conflict = await client.call_tool("rosetta_commit_delivery", {
                            "session_id": stale["session_id"],
                        })
                        self.assertTrue(conflict.isError)
                        self.assertEqual(target.read_text(encoding="utf-8"),
                                         "# edited outside Rosetta\n")
                        escape = await client.call_tool("rosetta_create_artifact", {
                            "pair_name": "Bash / Nushell (experimental)",
                            "target_path": str(root.parent / "outside.nu"),
                            "initial_target_content": "",
                        })
                        self.assertTrue(escape.isError)
                        created = unpack(await client.call_tool("rosetta_create_artifact", {
                            "pair_name": "Bash / Nushell (experimental)",
                            "target_path": "created.nu", "initial_target_content": "",
                        }))
                        new_view = (
                            "cat data/access.log | grep POST | wc -l\n"
                            "# @NU-PASSTHROUGH\n"
                            "# print \"native\"\n"
                            "# @END-NU-PASSTHROUGH\n"
                        )
                        unpack(await client.call_tool("rosetta_write_agent_view", {
                            "session_id": created["session_id"], "content": new_view,
                        }))
                        new_preview = unpack(await client.call_tool("rosetta_preview_delivery", {
                            "session_id": created["session_id"],
                        }))
                        self.assertIn('print "native"', new_preview["target_content"])
                        unpack(await client.call_tool("rosetta_commit_delivery", {
                            "session_id": created["session_id"],
                        }))
                        self.assertIn('print "native"',
                                      (root / "created.nu").read_text(encoding="utf-8"))
        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()

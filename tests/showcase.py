"""Demonstrate the installed MCP package using a separate user workspace."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from importlib.resources import files
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def unpack(result):
    if result.isError:
        raise RuntimeError("; ".join(item.text for item in result.content if hasattr(item, "text")))
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured.get("result", structured)
    return json.loads(result.content[0].text)


async def edit(client, pair: str, path: str, old: str, new: str) -> dict:
    session = unpack(await client.call_tool("rosetta_open_artifact", {
        "pair_name": pair, "target_path": path,
    }))
    view = session["agent_view"]
    if old not in view:
        raise AssertionError(f"Expected {old!r} in {pair} agent view")
    edited = view.replace(old, new, 1)
    unpack(await client.call_tool("rosetta_write_agent_view", {
        "session_id": session["session_id"], "content": edited,
    }))
    preview = unpack(await client.call_tool("rosetta_preview_delivery", {
        "session_id": session["session_id"],
    }))
    if not preview["target_content"]:
        raise AssertionError("Empty delivery preview")
    unpack(await client.call_tool("rosetta_commit_delivery", {
        "session_id": session["session_id"],
    }))
    unpack(await client.call_tool("rosetta_close_session", {
        "session_id": session["session_id"],
    }))
    return {"pair": pair, "path": path, "before": view, "after": edited,
            "target": preview["target_content"]}


async def main(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    workspace = root / f"rosetta_demo_{uuid.uuid4().hex[:8]}"
    workspace.mkdir()
    examples = files("rosetta_studio").joinpath("examples")
    for folder in ("nushell", "typst", "prql", "systemverilog"):
        shutil.copytree(str(examples.joinpath(folder)), workspace / folder)

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "rosetta_studio.mcp_server", "--workspace", str(workspace)],
        cwd=str(workspace),
        env=dict(os.environ),
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as client:
            await client.initialize()
            listed = unpack(await client.call_tool("rosetta_list_pairs", {}))
            assert len(listed) == 4
            nu = await edit(client, "Bash / Nushell (experimental)",
                            "nushell/target.nu", "grep GET", "grep POST")
            assert "group-by method" in nu["target"]
            tex = await edit(client, "LaTeX / Typst", "typst/target.typ",
                             "Quarterly update", "Release update")
            assert "Release update" in tex["target"]
            sql_session = unpack(await client.call_tool("rosetta_open_artifact", {
                "pair_name": "PRQL / SQL", "target_path": "prql/target.prql",
            }))
            sql_view = sql_session["agent_view"]
            updated_sql = re.sub(r"\b5\b", "3", sql_view, count=1)
            assert updated_sql != sql_view
            unpack(await client.call_tool("rosetta_write_agent_view", {
                "session_id": sql_session["session_id"], "content": updated_sql,
            }))
            sql_preview = unpack(await client.call_tool("rosetta_preview_delivery", {
                "session_id": sql_session["session_id"],
            }))
            assert "take 3" in sql_preview["target_content"]
            unpack(await client.call_tool("rosetta_commit_delivery", {
                "session_id": sql_session["session_id"],
            }))
            unpack(await client.call_tool("rosetta_close_session", {
                "session_id": sql_session["session_id"],
            }))
            sv = await edit(client, "Verilog / SystemVerilog", "systemverilog/target.sv",
                            "module counter", "module counter_demo")
            assert "module counter_demo" in sv["target"]

    nu_bin = shutil.which("nu")
    if nu_bin:
        ran = subprocess.run([nu_bin, "target.nu"], cwd=workspace / "nushell",
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", check=True)
        assert "2" in ran.stdout and "GET" in ran.stdout and "POST" in ran.stdout
        print("Nushell runtime: POST count 2; GET/POST grouping preserved")
    typst_bin = shutil.which("typst")
    if typst_bin:
        subprocess.run([typst_bin, "compile", "target.typ"], cwd=workspace / "typst",
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", check=True)
        print("Typst runtime: compiled target.pdf")
    print(f"Workspace: {workspace}")
    print("MCP: four pairs opened, edited, previewed, committed; see Agent activity")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-root", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.workspace_root.resolve()))

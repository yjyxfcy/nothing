# Rosetta compatibility layer

Rosetta lets a DeepSeek Harness agent edit a target file through a familiar
representation. For example, it can work with a Typst document through a
LaTeX view; you review the translated Typst preview before writing the file.
Rosetta Studio can display the editing steps and views.

| Your file | Agent view | External tool |
|---|---|---|
| Typst (`.typ`) | LaTeX | Tylax `t2l`, Pandoc; Typst for validation |
| SystemVerilog (`.sv`) | Verilog | `sv2v` |
| PRQL (`.prql`) | SQL | Installed with the Python package |
| Nushell (`.nu`) | Bash | Nushell for validation; experimental subset |

## Install for DeepSeek Harness (Windows)

1. Install Python 3.10+, DeepSeek Harness, [uv](https://docs.astral.sh/uv/getting-started/installation/), and the external tools for your file type. [Downloads and configuration](QUICKSTART.md#install-converters) explain which are required.
2. Clone this repository and open PowerShell in the project you want the agent to edit. Replace the path below with your clone:

   ```powershell
   $RosettaRepo = 'C:\path\to\Rosetta-for-AI-Agents'
   $Setup = Join-Path $RosettaRepo 'integrations\deepseek_harness\setup.ps1'
   & $Setup
   ```

3. In DeepSeek Harness, use the plugin manager to install the `plugin-current` path printed by setup, enable the plugin if needed, and restart Harness. See [the complete walkthrough](QUICKSTART.md#install-the-plugin-in-harness) for the manager command and verification.
4. Open a chat in the configured project and ask the agent to run `ros_doctor`. Check that your format's tools are available.

Setup uses `uv` when it is available and pip otherwise. It creates a dedicated
Python environment; it does not install external converters or modify the
Harness profile. The first installation needs network access. To install for a
project other than the current PowerShell directory, use `& $Setup -Workspace
'C:\path\to\your-project'`.

## Make your first edit

Create or choose a file inside the configured project. In a Harness chat, ask:

> Use Rosetta to change the heading in `docs/report.typ`, preserve its Typst-only content, and show me a preview. Do not deliver until I confirm.

Open the Rosetta status panel below the chat to inspect the familiar view and
target preview. **Preview does not write the file.** Click **Deliver**, review
the diff, and click **Confirm delivery** to save. Click **Release** when you
are finished. See [the quick start](QUICKSTART.md#edit-a-file) for details and
how to open the Studio activity window.

## Other installation options

For the standalone Python package, create and activate a virtual environment,
then run `uv pip install .` from the repository root (or `python -m pip install .`).
The GUI also requires Tkinter. For its MCP and Python API entry points, see below.
This runtime distribution includes example files; it does not contain the paper's
benchmark tasks or evaluation results.

## Connect an agent

Start the stdio MCP server from an MCP-capable host with `rosetta-mcp
--workspace C:\path\to\your\workspace`. Its tools list supported pairs,
open or create target artifacts, expose the familiar agent view, accept edits,
preview a target-format delivery, and commit it atomically. The agent must
call the MCP tools explicitly; this is not a transparent filesystem mount.
The server cannot intercept a host's ordinary file reads or writes. Existing
pass-through blocks in an opened view must remain unchanged. A commit requires
a preview of the current view; editing the view invalidates that preview.
Blank edits and empty deliveries are refused. New sessions may start empty,
but the agent must write content before previewing or committing them.
Delivery that is known to fail the target compiler is refused before commit.
Pending views are held in the MCP server process and do not survive its restart.
The configured workspace is the read/write boundary. Set
`ROSETTA_EVENTS_DB` to a shared SQLite path to show MCP steps in the GUI.
By default, events are stored under `~/.rosetta/events.sqlite3`.

For a stdio MCP host, set the command to `rosetta-mcp` and arguments to
`["--workspace", "C:\\path\\to\\your\\workspace"]`. Use the absolute path
to the installed command if the host does not inherit your virtual
environment's `PATH`. The workspace may be any directory you choose; it
does not need to contain the Rosetta source tree.
Some MCP hosts pass a restricted environment to their server subprocesses.
When using `ROSETTA_SV2V`, `ROSETTA_TYLAX`, or `ROSETTA_PANDOC`, configure
those variables in the MCP server's environment explicitly.

Rosetta performs projection and delivery locally with its adapters and the
configured converter executables; it does not send artifact contents to an
LLM for translation. When Rosetta is used through an online agent host, that
host's own handling of prompts and tool results still applies. Local session
and activity records may contain artifact text. The DSH plugin stores them in
`<workspace>/.rosetta/`; MCP activity defaults to `~/.rosetta/events.sqlite3`.

Launch the optional GUI with `rosetta-studio`. Its Examples menu contains
four small demos. Open any supported target file to use your own artifact.
The GUI's Agent activity window reads the shared event database. It can show
both MCP and DeepSeek Harness plugin sessions when each uses that workspace's
database.

The adapter API can also be used without MCP or the GUI:

```python
from rosetta_studio.core import available_pairs

pair = available_pairs()["PRQL / SQL"]
projection = pair.project("from employees\nselect name\n")
if projection.success:
    delivery = pair.deliver(projection.content)
```

For batch work, the optional `FileProjector` API maintains a separate shadow
directory. Failed projections are omitted rather than copied under the familiar
extension. `setup(force=True)` refreshes managed files but does not delete the
shadow directory; it can overwrite agent-edited projections and mirrored
dependencies, so save those edits first. Unmanaged files in the shadow directory
are preserved. Symlinked source files are omitted, and sync operations reject
paths that resolve outside their configured workspace or shadow directory.

## Current scope

Bash/Nushell handles a documented syntax subset and is marked experimental.
The Typst adapter uses Tylax for document conversion and Pandoc for recognized
list fragments. It supports nested and ordered lists, function-form list calls,
LaTeX `itemize`/`enumerate`, and fenced code inside list items. Line-initial
`#figure` and `#image` calls are preserved as protected native blocks; edit
those directly in Typst. If a backend loses a native block's position, Rosetta
tries another backend and refuses delivery when it cannot place the block
safely. Ambiguous or malformed list syntax is refused instead
of flattened silently. Formula conversion depends on the backend and the
expression; inspect rendered formulas as well as compiler results.
SQL delivery supports a conservative SELECT/JOIN subset, grouped aggregates,
selected-alias sorting, and validated CTE/UNION forms. Casts and query shapes
outside its scoped structural checks are refused; these checks are not a general
SQL-equivalence proof.
The SystemVerilog adapter does not reconstruct task-specific flattened
interfaces automatically. An interface-only module that `sv2v` cannot project
is rejected rather than exposed as an empty agent view. Project `include`
directives are currently refused because the adapter cannot reconstruct their
dependencies after lowering. Numeric object-like macros, `timescale`, and
source comments are carried through the `sv2v` projection path; supported
numeric macro values remain editable and affect the delivered widths. The
adapter refuses conditional/function-like macro definitions, repeated macro
definitions, and other preprocessor constructs it cannot reconstruct safely.
When comment placement cannot be retained beside its original construct, the
adapter reports that comments were relocated. Inspect and validate all
delivered artifacts with their target tools before relying on them.

These checks are scoped safeguards, not general proofs of semantic equivalence.
The package does not claim the coverage or success rates of the paper's
evaluation harness.

## License

Rosetta's own source files are licensed under the MIT License. Runtime
dependencies retain their own licenses; in particular, the Bash/Nushell
adapter depends on the separately installed GPL-licensed `bashlex` package.
See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

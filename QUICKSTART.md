# Use Rosetta with DeepSeek Harness

Rosetta lets an agent edit a Typst, SystemVerilog, PRQL or Nushell file through
a LaTeX, Verilog, SQL or Bash view. The file on disk remains in its original
format. The agent uses the `ros_*` tools; the Harness panel shows the two views
and lets you preview, deliver or release a session.

## Set up on Windows

Install Python 3.10+ and DeepSeek Harness, and clone this repository. In
PowerShell, set `$RosettaRepo` to the clone directory. Run setup while the
current directory is the project you want Rosetta to manage:

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) first for the recommended installation path. The first setup needs access to PyPI. Setup automatically uses uv when available; otherwise it uses pip. Run from the directory containing the files you want to edit:

```powershell
$RosettaRepo = 'C:\path\to\Rosetta-for-AI-Agents' # replace with your clone
$Setup = Join-Path $RosettaRepo 'integrations\deepseek_harness\setup.ps1'
& $Setup
```

Setup creates its own Python environment and plugin bundle. If pip stalls, install uv, reopen PowerShell so `uv` is on PATH, and rerun the same command. To use a custom pip index explicitly, pass `-PipArgs '--index-url https://pypi.org/simple --timeout 15 --retries 0'`; this selects pip even when uv is installed.

By default, setup manages the current PowerShell directory. If PowerShell is
open elsewhere, specify the project explicitly:

```powershell
& $Setup -Workspace 'C:\work\my-project'
```

The script installs Rosetta into its own local Python environment and prepares
a stable plugin bundle for that workspace. It prints the bundle path. Install
that directory as a local bundle with the Harness plugin manager
(`install_bundle`) **once**, then restart Harness. The script leaves your
existing Harness profile alone.

If `<project-directory>/.rosetta/.gitignore` does not exist, setup creates it to
keep local session, activity, and recovery data out of Git. An existing ignore
file is preserved. A linked `.rosetta` directory or `.gitignore` file is not
written through; check the ignore rules for its target before publishing.
Ignore rules do not untrack files already committed; check
`git ls-files -- .rosetta` before publishing an existing workspace.

Choose the narrowest project directory you intend the agent to edit. The
configured workspace is Rosetta's file-access boundary, so setting it to a
parent directory also makes its child projects reachable.

On later updates, run setup again and just restart Harness. The bundle path is
stable (`plugin-current`), so setup overwrites it in place instead of creating a
second bundle; do not call `install_bundle` a second time.

If you only changed the workspace or installed a converter and want to refresh
the generated settings without contacting PyPI, run setup with
`-SkipPackageInstall`. It verifies that the installed Python package matches
the repository source byte for byte before updating the plugin bundle. If the
Python source changed, use the normal setup command instead.

The plugin needs the external converter for the pair you plan to use:

| Target file | Familiar view | External tool |
|---|---|---|
| `.typ` | LaTeX | [Typst](https://github.com/typst/typst/releases) for validation; [Tylax `t2l`](https://github.com/scipenai/tylax) for whole-document projection and [Pandoc](https://github.com/jgm/pandoc/releases) for recognized list fragments. Install both for the documented broader coverage; without either backend, only the built-in subset is available. |
| `.sv` | Verilog | [sv2v](https://github.com/zachjs/sv2v/releases) |
| `.prql` | SQL | Python dependencies installed by the setup script |
| `.nu` | Bash | [Nushell](https://github.com/nushell/nushell/releases) for validation; this adapter covers a limited syntax subset |

Put installed commands on `PATH` before running setup. For converters outside
`PATH`, pass their executable paths directly; explicit paths are validated and
saved in the local plugin config:

```powershell
& $Setup -Workspace 'C:\work\my-project' -Sv2vPath 'D:\tools\sv2v.exe' -TypstPath 'D:\tools\typst.exe'
```

Available options are `-T2lPath`, `-PandocPath`, `-Sv2vPath`, `-TypstPath`, and
`-NushellPath`. Explicit arguments override `PATH` and previously saved paths.
The script reports which converters it found or configured. The plugin's
`ros_doctor` tool reports readiness after restart. If you install a converter
later, run setup with `-SkipPackageInstall` and restart Harness; the stable
bundle is updated in place and no new plugin installation is needed.

Rosetta stores local sessions, activity, and restore copies under the workspace's
`.rosetta/` directory; these records can contain artifact text. Keep that
directory out of public repositories when the project is private or sensitive.
DeepSeek Harness separately handles the prompts and tool results sent to its
service.

## Edit a file

Start a chat in the configured workspace by sending the request; Harness does
not mount the Rosetta panel on an unsent new-chat screen.

Ask the Harness agent, for example: "Use Rosetta to change the heading in
`docs/report.typ` while preserving its Typst-only blocks." The plugin's prompt
guidance directs the agent to open the file with `ros_open_artifact`, edit the
familiar view and create a delivery preview. The status panel below the chat
shows the active file. Expand it to see the views and the **Preview**,
**Deliver** and **Release** buttons. Preview does not change the file. Clicking
**Deliver** opens an in-panel confirmation with the target-file diff; the file is
written only after you click **Confirm delivery**. The diff shows textual changes
and does not verify that the requested behavior is correct. The agent cannot
commit from a model-supplied confirmation flag. Release discards any pending view
edit and lets ordinary file tools write the target again.

An idle session releases its live file protection after about 30 minutes, but a
dirty draft is not deleted by cleanup. The panel says "idle — write fence
released, draft kept"; the draft remains in the session store so you can inspect
or resume it. If the target changed on disk, Rosetta refuses to overwrite it.
Reopen the target to incorporate the new version.

## Watch the editing steps in Rosetta Studio

After setup, launch Studio from PowerShell:

```powershell
& (Join-Path $env:LOCALAPPDATA 'Rosetta\Harness\venv\Scripts\rosetta-studio.exe')
```

Click **Select workspace**, choose the same project directory configured in
the plugin, then open **Agent activity**. If you opened a target file in Studio,
it also looks for the workspace activity log automatically. The Studio editors
are for manual conversion; the Harness panel is the agent editing workflow.
By default, the activity log contains step metadata, not document snapshots, so
the timeline can show actions while its text panes remain empty. The Harness
panel still shows the current views. To record snapshots in Studio, set
`activityMode: full` in the plugin configuration; see the plugin README's
local-data section before enabling it.

The full plugin tool list and configuration are in
[`integrations/deepseek_harness/README.md`](integrations/deepseek_harness/README.md).

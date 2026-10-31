# Use Rosetta with DeepSeek Harness

Rosetta lets an agent edit a Typst, SystemVerilog, PRQL or Nushell file through
a LaTeX, Verilog, SQL or Bash view. The file on disk remains in its original
format. The agent uses the `ros_*` tools; the Harness panel shows the two views
and lets you preview, deliver or release a session.

## Set up on Windows

Install Python 3.10+, DeepSeek Harness, and Node.js with `pnpm` on `PATH`.
[uv](https://docs.astral.sh/uv/getting-started/installation/) is recommended
for the Python install. The first install needs network access to GitHub and
PyPI.

Close DeepSeek Harness, open PowerShell in the project directory you want
Rosetta to manage, and run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/yjyxfcy/nothing/main/install.ps1 | iex"
```

This downloads the repository into a temporary directory, creates a dedicated
Python environment under `%LOCALAPPDATA%\Rosetta\Harness`, writes the stable
`plugin-current` bundle, registers that bundle in the active DeepSeek Harness
desktop profile, and updates the profile lockfile. It backs up the profile's
`package.json` and `pnpm-lock.yaml` under
`.plugin-manager\rosetta-install-backup-*` before changing them. It does not
restart Harness.

Restart Harness, open a chat in the configured project, and ask the agent to
run `ros_doctor`; check that the tool pair you need is available.

If you prefer to inspect the installer first, clone the repository and run it
directly. If the clone is already the project directory, omit `-Workspace`:

```powershell
git clone https://github.com/yjyxfcy/nothing
powershell -NoProfile -ExecutionPolicy Bypass -File .\nothing\install.ps1 -Workspace 'C:\work\my-project'
```

Useful options:

```powershell
# Choose one or more project roots.
.\install.ps1 -Workspace @('C:\work\project-a', 'D:\work\project-b')

# Record converter binaries that are not on PATH.
.\install.ps1 -Workspace 'C:\work\my-project' -Sv2vPath 'D:\tools\sv2v.exe' -TypstPath 'D:\tools\typst.exe'

# Use a specific pip index or an offline/warmed cache.
.\install.ps1 -PipArgs '--index-url https://pypi.org/simple --timeout 15 --retries 0'

# Refresh the bundle without reinstalling Python, after changing workspace or converter settings.
.\install.ps1 -SkipPackageInstall

# Print the bundle path and use the in-app plugin manager instead of editing the profile.
.\install.ps1 -NoProfileInstall
```

If pip stalls, install uv, reopen PowerShell so `uv` is on `PATH`, and rerun
the installer. Setup uses uv when it is available and pip otherwise.

By default, setup manages the current PowerShell directory. Choose the
narrowest project directory you intend the agent to edit. The configured
workspace is Rosetta's file-access boundary, so setting it to a parent
directory also makes its child projects reachable.

If `<project-directory>/.rosetta/.gitignore` does not exist, setup creates it to
keep local session, activity, and recovery data out of Git. An existing ignore
file is preserved. A linked `.rosetta` directory or `.gitignore` file is not
written through; check the ignore rules for its target before publishing.
Ignore rules do not untrack files already committed; check
`git ls-files -- .rosetta` before publishing an existing workspace.

On later updates, run the installer again and restart Harness. The bundle path
is stable (`plugin-current`), so it overwrites the bundle in place instead of
creating a second one.

## Register the plugin manually

The installer already edits the active profile. Use this fallback only when
you passed `-NoProfileInstall`, when the profile lives outside the default
`%USERPROFILE%\.dsh\profiles\desktop`, or when you want the plugin manager to
own the registration.

`integrations\deepseek_harness\setup.ps1` creates the Python environment and
bundle and prints the bundle path, but deliberately leaves the Harness profile
alone:

```powershell
$Setup = 'C:\path\to\nothing\integrations\deepseek_harness\setup.ps1'
& $Setup -Workspace 'C:\work\my-project'
```

Then install the printed directory as a local bundle, enable it if needed, and
restart Harness:

```text
plugin_manager { action: "install_bundle", target: "<printed plugin-current path>" }
```

The repository root now declares the same bundle, so a plugin-manager install
from `github:yjyxfcy/nothing` also resolves. That installs the JavaScript half
only; run setup first so the Python environment and `plugin-current` bridge
exist.

If only the workspace or a converter changed and you do not want to contact
PyPI, run setup with `-SkipPackageInstall`. It verifies that the installed
Python package matches the repository source byte for byte before refreshing
the bundle. If the Python source changed, use the normal setup command
instead.

## Install converters

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

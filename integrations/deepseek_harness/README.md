# Rosetta for DeepSeek Harness

Rosetta lets a DeepSeek Harness agent edit selected target formats through a familiar representation. The target file stays in its native format on disk; the agent explicitly opens a Rosetta session, edits the returned view, and requests a translated preview. The user clicks **Deliver**, reviews an in-panel target-file diff, then clicks **Confirm delivery** to write it.

This integration is a Harness control plane for the separately installed `rosetta-compat-layer` Python package. It does not duplicate translation logic.

For the simplest installation, install [uv](https://docs.astral.sh/uv/getting-started/installation/) and follow the [quick start](../../QUICKSTART.md). Setup selects uv automatically when available; supplying `-PipArgs` selects pip. Python 3.10+ and DeepSeek Harness are still required, as are the external converters for the formats you use.

## Requirements

- Windows 10/11 and DeepSeek Harness with its local plugin manager.
- Python 3.10 or newer, available as `py -3` or `python`.
- Internet access to PyPI is required on first setup to download the Python build backend and package dependencies; installation time depends on the network or proxy. Setup keeps Hatchling in the dedicated environment so later local-source updates do not need to fetch the build backend into a temporary isolated environment again.
- Node.js is needed only for the automated acceptance tests; Harness includes a Node runtime that the test runner can discover.
- External tools depend on the pair you use:

| Target file | Agent view | Required or recommended tool |
|---|---|---|
| `.typ` | LaTeX | Typst for validation; Tylax `t2l` handles whole-document projection and Pandoc handles recognized list fragments. Install both for the documented broader coverage; either can still help with its supported path, while the built-in fallback covers less syntax. |
| `.sv` | Verilog | `sv2v` is required. |
| `.prql` | SQL | The Python package installs the PRQL compiler dependency. |
| `.nu` | Bash | Nushell for validation. This pair is experimental and supports a limited syntax subset. |

Install external tools from their maintainers: [Typst](https://github.com/typst/typst/releases), [Tylax](https://github.com/scipenai/tylax), [Pandoc](https://github.com/jgm/pandoc/releases), [sv2v](https://github.com/zachjs/sv2v/releases), and [Nushell](https://github.com/nushell/nushell/releases). Put their executables on `PATH` before setup, pass explicit paths to setup, or configure paths in the generated plugin config. Explicit setup paths are checked and override both `PATH` discovery and previously saved paths.

## Install

Open PowerShell in the project directory you want Rosetta to manage, then run the setup script by its path. With no `-Workspace` argument, the script uses PowerShell's current directory; running it from the Rosetta integration subdirectory would restrict access to that subdirectory. You can instead pass one or more workspace directories explicitly and run setup from anywhere. The script creates a dedicated Python environment under `%LOCALAPPDATA%\Rosetta\Harness`, installs the package from the Rosetta repository root, copies the plugin bundle to a stable `plugin-current` directory, and writes the selected workspace roots into the plugin configuration. Re-running setup with a new list replaces the configured allow-list, so include every project you want the plugin to manage. Roots are canonicalized and duplicate paths are removed. Studio's default workspace remains the first root; use **Select workspace** in Studio to switch its activity view. Setup also saves detected converter paths locally and saves the first workspace for Studio. Validation tools (`typst`, `nu`) are saved alongside translation tools (`t2l`, `pandoc`, `sv2v`), because the Harness process may not inherit a shell's updated `PATH`. Setup prints a per-pair readiness summary, distinguishing blocked pairs from the built-in reduced-coverage fallback and optional validators. The checked-in `cordis.patch.yml` intentionally contains no machine-specific paths; the Host can derive the workspace from the current Harness chat, while setup generates explicit roots for this machine.

For every selected root, setup creates `<workspace>/.rosetta/.gitignore` when missing, to exclude local sessions, activity, and recovery data from Git. An existing ignore file is left unchanged; verify that it excludes `.rosetta` state before publishing the workspace. Setup will not write through a reparse-point `.rosetta` directory or `.gitignore` file; if it warns, protect the link target through the repository's ignore rules. Ignore rules do not untrack files already committed, so check `git ls-files -- .rosetta` before publishing an existing workspace.

```powershell
# Run this while the current directory is the project to manage; replace the
# placeholder with the cloned Rosetta repository path.
$setupScript = '<path-to-rosetta-repo>\integrations\deepseek_harness\setup.ps1'
powershell -NoProfile -ExecutionPolicy Bypass -File $setupScript
# Or run from any directory and name the workspace explicitly:
powershell -NoProfile -ExecutionPolicy Bypass -File $setupScript -Workspace 'C:\work\my-project'
# Multiple roots are supported; rerunning replaces the previous allow-list:
& $setupScript -Workspace @('C:\work\project-a', 'D:\work\project-b')
# Converter binaries outside PATH can be passed directly (all are optional):
& $setupScript -Workspace 'C:\work\my-project' -Sv2vPath 'D:\tools\sv2v.exe' -TypstPath 'D:\tools\typst.exe'
# Offline or warmed cache (requires Hatchling and runtime dependencies in the venv or wheel cache):
powershell -NoProfile -ExecutionPolicy Bypass -File $setupScript -PipArgs '--no-index --no-deps --no-build-isolation'
# If your configured package index is unavailable, use public PyPI explicitly:
powershell -NoProfile -ExecutionPolicy Bypass -File $setupScript -PipArgs '--index-url https://pypi.org/simple --timeout 15 --retries 0'
# Refresh only workspace/converter settings and the Host bundle; verify Python source is already installed:
powershell -NoProfile -ExecutionPolicy Bypass -File $setupScript -SkipPackageInstall
```

The recommended uv setup was verified in a fresh Python 3.11 environment. If your network requires an organization-managed package mirror, configure uv or pass the appropriate pip index with `-PipArgs`.

The script does not edit the Harness profile. On the first install, use the Harness plugin manager to install the printed bundle path:

```text
plugin_manager { action: "install_bundle", target: "<printed plugin-current path>" }
```

Restart Harness after installation. For later code updates, rerun `setup.ps1` and restart Harness; it updates the same bundle directory, so do not install a second copy. If only the workspace or converter paths changed, use `-SkipPackageInstall`: it skips pip and first verifies that the installed Python packages match `src/` byte for byte. It refuses to refresh a stale Python package, so use the normal setup command when Python source changed. This path also avoids contacting PyPI when adding a converter or switching workspaces. Running setup again is safe and idempotent: it reuses the existing Python environment, overwrites `plugin-current` in place, and does not add a second bundle. It never installs into the Harness profile and never restarts Harness for you.

If an older installation points at this repository's `integrations/deepseek_harness` source directory rather than `plugin-current`, Harness cannot replace the same plugin ID in place. Back up the Harness profile, uninstall that old Rosetta entry in the plugin manager, install the generated `plugin-current` bundle, enable it, and restart. Keep the existing workspace's `.rosetta/` directory; uninstalling the plugin does not require deleting sessions or artifacts. To update a converter installed outside `PATH`, rerun setup with its matching `-T2lPath`, `-PandocPath`, `-Sv2vPath`, `-TypstPath`, or `-NushellPath` argument; the generated plugin configuration records the resolved executable path.

### First-run checklist

1. Install the external tools your pair needs and put them on `PATH`.
2. Open PowerShell in the project directory you want to manage and run setup using the Rosetta repository's script path, or pass `-Workspace <path1>,<path2>` explicitly. The no-argument default is the shell's current directory. It prints the stable bundle path and each allowed root.
3. In Harness, run `plugin_manager { action: "install_bundle", target: "<printed path>" }` and enable Rosetta if the manager does not enable it automatically.
4. Restart Harness. A plugin cannot be loaded into the running process.
5. Send the first message in a chat inside the workspace. The status strip appears below the composer only after a chat exists; an unsent blank chat does not mount it.
6. Ask the agent to run `ros_doctor`. This is the one check that proves the Host and Python halves agree.

There is no supported one-click install, and this repository does not claim one: the Harness plugin manager step and the restart are performed by the user.

### Did the running Harness actually load this build?

`check_activation.ps1` answers four different questions separately, and only reports `ACTIVE` when all of them hold:

1. is a bundle installed at `%LOCALAPPDATA%\Rosetta\Harness\plugin-current`?
2. do its `index.js`, `client.js`, and `python/rosetta_harness_bridge.py` hashes equal the source files next to the script? (`INSTALLED BUT STALE` means run `setup.ps1`.)
3. is the status route reachable?
4. does the **running** Host advertise the same generation digest as the installed bundle, and list `ros_maintenance`?

The generation digest is computed from those three files at load time and returned as the `x-rosetta-generation` header on the status route, including on its 401 response. A 401 alone proves only that *some* module from the current generation is live; it does not prove that the installed bundle is that module. The script therefore distinguishes `SYNCED - RESTART REQUIRED` from `ACTIVE`, and never treats process start times or a 401 as proof. The browser's loaded `client.js` is still outside what the Host can observe; restart and hard-refresh, then confirm the short `Host bundle` value in the strip's Details matches the digest the script prints.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\check_activation.ps1
```


After restart, ask the agent to run `ros_doctor`. It reports the active Python package, allowed workspace, converter availability, blocked pairs, and `activityMode` explicitly as the activity-log mode. `metadata` is the default; `full` stores text snapshots, and `off` disables activity rows. A missing optional converter may reduce Typst coverage; a missing `sv2v` blocks SystemVerilog.

## Use

Ask the agent to edit a supported target, for example:

> Use Rosetta to update the heading in `docs/report.typ`, preserve all Typst-only blocks, and show me the delivery preview.

For a new target file, ask the agent to create it through Rosetta in the familiar language. The `ros_create_artifact` tool accepts `initialAgentView` (LaTeX, Verilog, SQL, or Bash) and converts it to the target format during preview; the new file remains absent until you confirm delivery. Existing integrations may still pass `initialTargetContent` when they already have target-format text. New-file creation cannot invent target-native pass-through content safely: create a native target file first, then open it with Rosetta if the artifact needs such blocks.

The agent opens the target with `ros_open_artifact`, edits only the returned familiar view, and requests a preview. The Rosetta status strip below the composer shows the session and candidate. Expand it to inspect the familiar view and target preview. **Preview does not write the file.** Click **Deliver** to open a confirmation that shows the target-file diff; click **Confirm delivery** to write, or **Cancel** to leave the file unchanged. The diff is textual and does not verify that the requested behavior is correct. The agent cannot commit by claiming that the user confirmed. Click **Release** to discard pending edits and return the path to ordinary file editing.

Harness does not mount the status strip on an unsent new-chat screen. Send the first message in a workspace to create the chat; the strip then appears. With no configured workspace, it shows a setup prompt instead of a service error, and the first agent open can derive the current chat's workspace. The strip also cannot read files for you: ordinary file reads still show the native target representation, and the agent must use the Rosetta tools.

Supported target extensions are detected automatically: `.typ`, `.sv`, `.prql`, and `.nu`. The agent must use the Rosetta tools; ordinary file reads still show the native target representation. The status strip also has an **Open file** action for starting a session directly.

Rosetta marks target-only content as protected pass-through blocks in the familiar view. The agent can edit the translated portions around those blocks, but must not alter, remove, duplicate, or reorder the protected blocks. The plugin fences the Harness `write` and `edit` tools on a managed target while the session is active.

## Safety and recovery

- The Rosetta bridge performs conversion locally and does not send artifact
  contents to an LLM for translation. DeepSeek Harness still processes the
  agent's prompts and tool results according to its own service behavior.
- Delivery requires a preview of the current view and a human click in the Harness status strip. The click is bound to the preview, view, and target hashes; stale confirmation is rejected.
- Delivery writes through a same-directory temporary file and atomic replacement. A recovery journal handles a failure between publishing the file and updating session state; those two resources are not one joint transaction.
- A target changed by another process is detected as drift and will not be overwritten. Reopen it to work from the new version.
- **Idle releases the fence, not the draft.** Any session stops fencing the target after about 30 minutes without activity, including one with an undelivered draft or preview. After that, Harness `write`/`edit` on the path are allowed again. The draft and the stored preview remain in the local session store, and the status strip labels the session `idle — write fence released, draft kept`; the delivery confirmation repeats that the target hash will be checked. Reopen the artifact to work from the current file, or use the strip to preview and deliver. Do not read "idle" as "discarded", and do not expect the idle session to keep protecting the file.
- The write fence covers Harness `write` and `edit` only. Shell commands, scripts, other plugins, and external applications can still modify the file. Rosetta detects the resulting drift before delivery but cannot prevent those writes. There is no transparent filesystem mount and no read interception.
- The session store and activity log live under `<workspace>/.rosetta/`. They can contain artifact text and prior file contents needed for restore. Treat this directory as project data and do not publish it unless you intend to disclose that content; setup's ignore file does not remove data already tracked by Git.
- Studio remembers the workspace selected during setup; use **Select workspace** to switch projects. Its Activity timeline hides high-frequency tool-discovery rows while retaining them in the local event database.

### Local data and retention

Rosetta keeps three kinds of local records under `<workspace>/.rosetta/`:

| Record | Default limit | What is never removed automatically |
|---|---|---|
| Activity log (`events.sqlite3`) | 30 days and 5,000 rows **written by this plugin** | Rows written by other Rosetta processes, including MCP. Share the file with Studio by pointing both at it. |
| Saved previous versions (`commit-history/`) | Newest 3 per session; 200 files / 128 MiB overall | The newest saved version of every session that still exists, which is the only one **Restore previous** can reach. |
| Session records (`harness-sessions.sqlite3`) | Clean records expire after 24 h; 200 clean records overall | Any session with an undelivered draft or a stored preview. A draft can push the count above the limit; that is reported, not forced. |

The activity log defaults to **metadata mode** so it does not duplicate source and view text into the shared event database. The session store and saved previous versions still retain text needed for an active draft and Restore; close or clean sessions only when you intend to discard those recovery records. Choose `full` only when you want the activity timeline's text snapshots, or `off` to disable activity logging.

Settings are optional and validated; an unknown type falls back to the default and an out-of-range number is clamped, instead of disabling the plugin. They can be set in the generated plugin config:

```yaml
activityMode: metadata        # metadata | full | off
activityRetentionDays: 30
activityMaxEvents: 5000
commitHistoryKeep: 3
commitHistoryMaxFiles: 200
commitHistoryMaxBytes: 134217728
staleDraftDays: 30
```

- `activityMode: metadata` keeps the step, status, target path, session id, a **known** pair name, and a fixed-shape error code. It stores **no** agent-view or target-view text, and no free-text error message or note: adapter errors and round-trip notes routinely quote the artifact, so they are replaced by a fixed summary such as `Step preview failed with DELIVERY_FAILED.` Identifiers that fail the workspace/known-pair/session-shape checks are dropped rather than stored. This is narrower than "no user text at all" - paths and ids remain - but it is the promise the code enforces. Studio's timeline and step/status display still work; the two text panes stay empty. `activityMode: full` opts into those text snapshots. `activityMode: off` writes no activity rows and creates no database. None of these modes affects projection, preview, delivery, recovery, or the status strip, which read the session store rather than the activity log.
- `ros_doctor` reports the accepted limits and the current counts, including how many drafts are held, how many live sessions still have an undo, and how many clean records a prune would remove.
- `ros_maintenance` is the explicit cleanup path. Its default is a read-only report; `action: "clean"` with `apply: true` applies the automatic policy. Undelivered drafts are included only when `includeDrafts: true` is also passed and they are older than `olderThanDays` (default 30). Backups for sessions that have ended or expired are reported separately as *orphans* (they cannot be reached by `ros_restore_last`) and are removed only when `includeOrphanHistory: true` is also passed, `apply: true` is set, and their newest delivery is older than `olderThanDays`. Every removal is reported by count and scope before it happens; the report is the preview. Pending commit-journal entries are reported but never cleaned, because they are unfinished deliveries that must still be replayed.
- Nothing in the automatic retention path deletes a pending preview, a dirty draft, the newest saved version of a live session, or a journal entry. That is why the counts can stay above a ceiling: the policy refuses to trade a usable recovery point for a tidy number. When the ceiling is still exceeded, the report names the live sessions that hold it and tells you the sequence (`clean`, close/expire those sessions, `clean` with `includeOrphanHistory: true`).



## Boundaries

- Harness does not expose a read-interception hook or permit this plugin to rewrite file-read arguments. Rosetta is an explicit tool workflow, not a transparent filesystem mount.
- Workspace access is constrained to the configured workspace roots. Setup uses the selected roots; paths outside all of them are refused.
- Translation is syntax-subset based, not a general-purpose compiler or semantics-preserving transpiler. Inspect every preview and run the target toolchain for your project.
- For Typst, line-initial `#figure` and `#image` calls are preserved as protected native blocks. Edit those blocks directly in the target file. Nested or ordered lists can be refused. Formula conversion depends on the backend and expression; inspect rendered formulas, and use a native pass-through block where an unchanged formula must be preserved.
- When an isolated Typst check fails on a candidate containing a project import, include, bibliography or image, the Harness bridge retries compilation with files under the configured workspace available. Missing dependencies or a failed project compile still block delivery. Other Rosetta entry points may still report an isolated-check failure for multi-file projects.
- The round-trip status checks whether the delivered artifact projects back to the approved view as text; it does not verify the requested behavior. A Typst edit can report `unstable` when re-projection removes a blank line immediately after a LaTeX heading. The status strip identifies that narrow difference, but keeps the check inconclusive; inspect the target diff and validate it with Typst before delivery.
- The SystemVerilog adapter may emit `logic` and `always_ff` where the familiar Verilog view showed `wire` and `always @`. A text-exact round trip is not a hardware semantics check; validate generated designs with the simulator or linter used by your project.
- Individual artifacts are limited to 2 MB by the bridge.
- The Bash/Nushell pair is experimental. Target-native constructs are carried in protected blocks, and unsupported familiar syntax can be refused.
- A successful compiler check does not prove that the requested semantic change was made. Review the preview and validate the behavior of important artifacts yourself.
- Plugin code changes are loaded by Harness only after restart.

To keep an existing paragraph with a formula unchanged, surround the whole
paragraph with native block markers before opening the file in Rosetta:

```typst
// @TYPST-PASSTHROUGH
The relation is $ x^2 + y^2 = z^2 $.
// @END-TYPST-PASSTHROUGH
```

The marked paragraph is protected from edits in the familiar view. Edit it in
Typst directly when a formula or its surrounding prose needs to change.

## Verify locally

Run the acceptance suite from this directory:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\run_acceptance.ps1
# Run only the setup workspace-selection layer:
powershell -NoProfile -ExecutionPolicy Bypass -File .\run_acceptance.ps1 -OnlyLayer setup-workspaces
```

It first uses the Python environment created by `setup.ps1` (or `ROSETTA_TEST_PYTHON` when explicitly set), then falls back to the repository's `.verify` environment. Before running tests it compares every installed runtime file with `src/`; a stale environment is rejected instead of producing a misleading pass. It then runs fourteen layers: workspace selection and ignore-file safety; actionable error-recovery guidance; the standalone demo; Tylax multi-round idempotence; the root package, GUI, and adapter tests; projector safety; bridge JSON-contract acceptance; plugin regressions; commit recovery; control-plane gates; local-state retention; Host-plugin behavior against a fake Cordis context; fence integrity; and Client status-strip behavior. Run a focused workspace-setup or action-hints layer with `-OnlyLayer setup-workspaces` or `-OnlyLayer action-hints`. The setup-focused layer uses temporary directories only; it does not run setup against `%LOCALAPPDATA%`, invoke pip, or edit the Harness profile. The Tylax-specific layer is reported as skipped when Tylax is unavailable; it never passes by silently exercising the regex fallback. Logs go to `acceptance-reports/`. It creates a fresh randomly named workspace under this integration directory for generated test artifacts; it does not use or delete a user's project directory or depend on the system temporary directory. The generated workspace and reports are ignored by Git. Reports include machine-local executable and workspace paths; review them before sharing. The test suite does not replace a live Harness restart and UI smoke test: plugin activation, the real status strip, and clicking **Deliver** in the running desktop process are not covered by any layer here.

To run only the UI-model tests:

```powershell
node .\tests\client_status_test.mjs
```

The Python suites use the interpreter created by setup. Acceptance discovers converters from their `ROSETTA_*` environment variables, `PATH`, or the local paths saved by setup; it does not require machine-specific paths in the repository.

## Package contents

- `index.js`: Host plugin, tools, workspace policy, confirmation gate, generation identity, and authenticated status/action routes.
- `client.js`: Harness status strip, preview/delivery controls, and the short Host bundle digest in Details.
- `python/rosetta_harness_bridge.py`: JSON bridge to the installed Python package, including metadata-only logging and local-state retention.
- `setup.ps1`: local environment and bundle setup (`-PipArgs` for an offline/warmed install).
- `workspace_setup.ps1`: workspace validation/deduplication and guarded `.rosetta/.gitignore` initialization used by setup.
- `check_activation.ps1`: compares source, installed bundle and the running generation; only reports `ACTIVE` when all three agree.
- `tests/`: regression and acceptance tests, including `setup_workspace_test.ps1` for multi-root setup behavior.

The bundle excludes session databases, activity logs, generated workspaces, test reports, profile backups, credentials, and model transcripts.

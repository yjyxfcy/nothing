/**
 * Rosetta compatibility layer for DeepSeek Harness (Host half).
 *
 * What this plugin is
 * -------------------
 * A workspace-scoped *artifact* session. The user (or the agent, on the user's
 * instruction) picks a target artifact and a Rosetta tool pair. The plugin
 * projects the target into the agent's familiar view and hands that view to the
 * model. The model edits the view. `ros_preview_delivery` translates the edited
 * view back to the target format and shows it without writing anything. The
 * actual write happens only when the user clicks Deliver in the status strip;
 * that click is bound to the exact preview, view and target digests the user
 * confirmed, and re-checked before the atomic write.
 *
 * Where the translation happens
 * -----------------------------
 * Nowhere in this file. Every projection and delivery is performed by the
 * installed `rosetta_studio` Python package, invoked as a controlled local
 * process (`python/rosetta_harness_bridge.py`). This plugin is a control plane:
 * tool surface, path policy, confirmation gate, status surface.
 *
 * Why this is not a transparent filesystem hook
 * --------------------------------------------
 * Harness 0.2.0-rc.2 exposes no reader interception: there is no
 * `fs/read-intent` event, and `tools/pre-execute` deliberately cannot rewrite a
 * tool call's arguments. So the plugin *cannot* make `read` on `report.typ`
 * return LaTeX. The model must call the Rosetta tools, and this plugin is honest
 * about that in the tool descriptions rather than pretending otherwise.
 *
 * What it *can* guarantee is the other half, and it does: while a target is
 * under an active Rosetta session, `write` and `edit` are denied on that exact
 * path by a monotonic `ctx.tools.guard()`. The agent can never edit the
 * familiar view through Rosetta and silently overwrite the same target through
 * the plain file tools.
 */
import { spawn } from 'node:child_process'
import { createHash, randomUUID } from 'node:crypto'
import { existsSync, readFileSync, realpathSync } from 'node:fs'
import { homedir } from 'node:os'
import { basename, dirname, extname, isAbsolute, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const BRIDGE = join(HERE, 'python', 'rosetta_harness_bridge.py')

/**
 * Identity of the code this Host module actually loaded.
 *
 * `check_activation.ps1` compares this digest with the same digest computed from
 * `<LOCALAPPDATA>\Rosetta\Harness\plugin-current`. A 401 from the status route
 * proves only that *some* current-generation module is live; it does not prove
 * that the installed bundle is the one running. This digest does, because it is
 * computed from the bytes of the three files at load time and published as an
 * unauthenticated response header (a hash and tool names only - no artifact
 * text).
 *
 * The client half is included because a stale `client.js` is exactly the kind
 * of "installed but not really updated" state the check exists to catch. The
 * browser's loaded copy is a separate matter: the Host cannot see it, and the
 * check says so rather than implying more.
 */
const GENERATION_FILES = [
  ['index.js', join(HERE, 'index.js')],
  ['client.js', join(HERE, 'client.js')],
  ['python/rosetta_harness_bridge.py', BRIDGE],
]

function fileDigest(path) {
  try {
    return createHash('sha256').update(readFileSync(path)).digest('hex')
  } catch {
    return null
  }
}

function computeGeneration() {
  const files = {}
  const lines = []
  for (const [name, path] of GENERATION_FILES) {
    const digest = fileDigest(path)
    if (!digest) return { digest: null, files }
    files[name] = digest
    lines.push(`${name}:${digest}`)
  }
  const digest = createHash('sha256')
    .update(`${lines.join('\n')}\n`, 'utf8')
    .digest('hex')
  return { digest, files }
}

const GENERATION = computeGeneration()

/** Tool pairs the plugin can drive, discovered from the Python package. */
const PAIR_CHOICES = [
  'LaTeX / Typst',
  'Verilog / SystemVerilog',
  'PRQL / SQL',
  'Bash / Nushell (experimental)',
]

/** Target extensions, so opening a file does not require naming its pair. */
const PAIR_BY_EXTENSION = new Map([
  ['.typ', 'LaTeX / Typst'],
  ['.sv', 'Verilog / SystemVerilog'],
  ['.prql', 'PRQL / SQL'],
  ['.nu', 'Bash / Nushell (experimental)'],
])

/** Recovery advice for bridge refusals with a user-controlled next step. */
export const ACTION_HINTS = Object.freeze({
  PREVIEW_REQUIRED:
    'call ros_preview_delivery for the current view, show that candidate to the user, and ask them to review it before clicking Deliver.',
  INVALID_UNICODE:
    're-read the Rosetta view and retry with valid text; the view and target file were left unchanged.',
  PASSTHROUGH_CHANGED:
    'restore the marked block exactly as it was, then edit only the familiar text around it.',
  PASSTHROUGH_MALFORMED:
    'reopen the artifact with ros_open_artifact to get a clean view; do not remove or rewrite its marker lines.',
  TARGET_CHANGED:
    'inspect the newer on-disk file, then reopen it with ros_open_artifact and make a fresh preview before delivering.',
  TARGET_CHANGED_DURING_COMMIT:
    'the delivery was not published; inspect the newer target, reopen it, and preview again.',
  CONFIRMATION_STALE:
    'the candidate, view, or target changed after confirmation; preview the current view again and ask the user to review and click Deliver for that candidate. Nothing was written.',
  DELIVERY_DOES_NOT_COMPILE:
    'fix the construct named by the adapter, preview again, and ask the user to review the new candidate. Nothing was written.',
  DELIVERY_FAILED:
    'check ros_doctor for backend availability; if the backend is available, use the adapter error to simplify or correct the unsupported construct, then preview again.',
  DELIVERY_EMPTY:
    'check why translation produced no content; restore the intended view before retrying. Rosetta refused to replace existing content with an empty artifact.',
  ROUND_TRIP_NOT_STABLE:
    'the delivery was refused; inspect the round-trip details, revise the view if needed, then preview again.',
  ROUND_TRIP_REFUSED:
    'inspect the round-trip details and preview the current view again before asking the user to deliver.',
  FIDELITY_LOSS:
    'the adapter reports that content would be lost; simplify the construct named in its note, then preview again.',
  PROJECTION_FAILED:
    'check ros_doctor for the required converter; install or configure it if unavailable. If it is available, correct or simplify the construct named in the error and reopen the artifact.',
  PROJECTION_EMPTY:
    'verify that the target contains readable content and that the selected pair matches its format; Rosetta did not open a session or change the file.',
  PROJECTION_MALFORMED:
    'check the target syntax and pair, then correct the malformed pass-through markers or unsupported source construct before reopening.',
  TARGET_NOT_FOUND:
    'check the path and confirm the target still exists. If it was moved or deleted, restore it or use its current path. ros_open_artifact requires an existing file; for a new file, use ros_create_artifact. No file was created.',
  PARENT_MISSING:
    'create the parent directory first; ros_create_artifact does not create directories or the target file on this refusal.',
  EXTENSION_MISMATCH:
    'choose the pair whose target extension matches this file, or correct the path if it names the wrong file; check ros_list_pairs for pair extensions.',
  ARTIFACT_TOO_LARGE:
    'reduce the target or familiar-view content below 2,000,000 UTF-8 bytes (about 2 MB), then retry.',
  TARGET_EXISTS:
    'the create action never overwrites an existing path; use ros_open_artifact to edit that file, or choose a different unused target path.',
  INITIAL_VIEW_PASSTHROUGH:
    'a new file has no existing target-native block to carry through. Remove the pass-through markers and create from the familiar view, or create the native target file first and open it with Rosetta.',
  CREATE_CONTENT_AMBIGUOUS:
    'send either initialAgentView in the familiar language or initialTargetContent in the target language, not both.',
  NOT_UTF8:
    'save or convert the target file as UTF-8 (a UTF-8 BOM is accepted), then reopen it.',
  PATH_REQUIRED:
    'provide the artifact path inside a configured workspace root.',
  PATH_IS_PLUGIN_STATE:
    'choose a project artifact path; files under .rosetta are private plugin state and cannot be opened as artifacts.',
  WORKSPACE_ROOT_NOT_ABSOLUTE:
    'configure workspaceRoots with an absolute directory path.',
  WORKSPACE_ROOT_MISSING:
    'create or select an existing directory for workspaceRoots, then retry.',
  SESSION_ID_REQUIRED:
    'provide the session id returned by ros_open_artifact or ros_create_artifact.',
  AMBIGUOUS_SESSION:
    'use the full session id returned by Rosetta rather than a shortened prefix.',
  INVALID_ENCODING:
    'resend the tool request with valid UTF-8 text; the bridge did not run the command.',
  CONTENT_REQUIRED:
    'for ros_agent_view action "write", provide the complete familiar view in the content field; an omitted field is not treated as an empty edit.',
  HISTORY_UNAVAILABLE:
    'the delivery was not written because its undo copy could not be saved; check workspace .rosetta/commit-history permissions and free space, then preview and retry.',
  RESTORE_UNAVAILABLE:
    'there is no retained previous version to restore (for example, the target may have been new); no restore occurred. Use your own backup or version control.',
  RESTORE_TARGET_CHANGED:
    'the target has changed since Rosetta delivered it, so restore was refused to protect newer content. Save or compare the current file and resolve it manually; no restore occurred.',
  JOURNAL_UNAVAILABLE:
    'the delivery was not written because its recovery record could not be saved; check workspace .rosetta permissions and free space, then preview and retry.',
  TEMP_UNAVAILABLE:
    'make the workspace .rosetta directory writable so Rosetta can create its temporary file, then retry.',
  STORE_CORRUPT:
    'preserve a copy of the workspace .rosetta state before moving the damaged session store aside; then reopen the artifact.',
  STORE_UNREADABLE:
    'check read/write permissions on the workspace .rosetta directory, then retry the Rosetta action.',
  PATH_OUTSIDE_WORKSPACE:
    'use a target path inside one of the configured workspace roots.',
  NO_WORKSPACE_ROOT:
    'configure an absolute workspaceRoots path or reopen the project from a Harness chat that exposes its workspace.',
  UNKNOWN_SESSION: 'the session is no longer available; reopen the artifact with ros_open_artifact.',
  SESSION_OWNERSHIP_MISMATCH:
    'open the artifact in this chat and use the new session returned here; sessions cannot be used across chats.',
  CHAT_SESSION_REQUIRED:
    'use the Rosetta tools in a Harness chat with an active chat identity; the status strip cannot act without one.',
  UNKNOWN_PAIR: 'call ros_list_pairs and use one of the exact pair names it returns.',
  SESSION_CHANGED: 'the view changed during this action; read the current view and preview it again.',
})

function inferPair(targetPath) {
  if (typeof targetPath !== 'string') return ''
  return PAIR_BY_EXTENSION.get(extname(targetPath).toLowerCase()) || ''
}

/** Built-in file-mutation tools this plugin will fence off for a live target. */
const MUTATING_TOOLS = new Set(['write', 'edit'])

/** How long one bridge call may run before it is killed. */
const BRIDGE_TIMEOUT_MS = 120_000

export const name = 'rosetta-compat'

export const inject = ['tools', 'webServer']

/**
 * Config is resolved by hand rather than declared as a schema.
 *
 * A Cordis `Config` export must be a Standard Schema, and loading
 * `@deepseek-ai/schemastery` from a plain-JavaScript bundle is exactly the kind
 * of private-package dependency that breaks on a Harness upgrade. Every field
 * below therefore has a documented default, an unknown key is ignored, and a
 * wrong type falls back rather than aborting activation: the plugin reports what
 * it actually got through `ros_doctor` instead of refusing to load.
 */
const DEFAULTS = {
  pythonExecutable: '',
  sourceRoot: '',
  bridgePath: '',
  workspaceRoots: [],
  stateFile: '',
  // Shared Rosetta activity database the Studio GUI polls. Empty leaves the
  // bridge's own default: <workspace root>/.rosetta/events.sqlite3.
  eventsDb: '',
  // metadata | full | off. Metadata keeps each step and status without copying
  // artifact/view text into the shared activity log. Full snapshots are opt-in;
  // `off` writes nothing. None affects projection, delivery or the status strip.
  activityMode: 'metadata',
  activityRetentionDays: 30,
  activityMaxEvents: 5000,
  // Saved previous-version entries used by "Restore previous". The newest entry
  // per session is never removed automatically.
  commitHistoryKeep: 3,
  commitHistoryMaxFiles: 200,
  commitHistoryMaxBytes: 128 * 1024 * 1024,
  // Default age for the explicit maintenance cleanup of abandoned drafts.
  // Drafts are never removed automatically.
  staleDraftDays: 30,
  convert: {},
  requireStableRoundTrip: false,
  // Enforced by default: a commit must be backed by a preview of the current
  // view. Set false only for scripted use where nothing is shown to a human.
  requirePreview: true,
  // Where the "when to use Rosetta" section sits in the assembled prompt. The
  // Harness orders sections numerically; this plugin has no reserved slot, so the
  // value is configurable rather than guessed at compile time.
  promptSectionOrder: 900,
}

function booleanOr(value, fallback) {
  return typeof value === 'boolean' ? value : fallback
}

function finiteNumberOr(value, fallback) {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

/**
 * A configured integer limit, clamped into a range the bridge can rely on.
 *
 * Bad input falls back to the documented default rather than disabling the
 * plugin, which matches the rest of this config: `ros_doctor` reports what was
 * actually accepted, so the user can see that a typo was ignored.
 */
function intInRangeOr(value, fallback, low, high) {
  const number = typeof value === 'number' ? value : Number.NaN
  if (!Number.isFinite(number)) return fallback
  return Math.max(low, Math.min(high, Math.trunc(number)))
}

const ACTIVITY_MODES = new Set(['full', 'metadata', 'off'])

function activityModeOr(value, fallback) {
  if (value === false) return 'off'
  if (value === true) return 'full'
  if (typeof value !== 'string') return fallback
  const mode = value.trim().toLowerCase()
  if (mode === 'metadata-only' || mode === 'metadata_only' || mode === 'meta') {
    return 'metadata'
  }
  return ACTIVITY_MODES.has(mode) ? mode : fallback
}

function stringOr(value, fallback) {
  return typeof value === 'string' ? value : fallback
}

/**
 * The Harness chat that made this tool call, when the Host exposes it.
 *
 * `exec.agent` is `undefined` for scripted calls, and an agent is absent from
 * some internal dispatches. In that case the answer is `null`, not a new
 * synthetic id: attribution is evidence, and a made-up owner would let the
 * status strip show one chat's session as another chat's work.
 *
 * The field is the same stable Session identity the Host uses for its own
 * sandbox/approval routing (`exec.agent.session.id`); `agent.id` and
 * `agent.session.id` are required to be equal by the agent registry.
 */
function chatSessionIdFromExec(exec) {
  const id = exec && exec.agent && exec.agent.session && exec.agent.session.id
  return typeof id === 'string' && id.trim() ? id.trim() : null
}

function requireChatSessionFromExec(exec, operation) {
  const chat = chatSessionIdFromExec(exec)
  if (!chat) {
    throw new Error(
      `CHAT_SESSION_REQUIRED: ${operation} needs the Harness chat identity, but this ` +
        'call arrived without one. Nothing was changed. Use the Rosetta status strip or ' +
        'retry from a chat with a stable Session id.',
    )
  }
  return chat
}

function normaliseChatSessionId(value) {
  return typeof value === 'string' && value.trim() && value.trim().length <= 200
    ? value.trim()
    : ''
}

function normaliseConfig(raw) {
  const input = raw && typeof raw === 'object' ? raw : {}
  const convertInput = input.convert && typeof input.convert === 'object' ? input.convert : {}
  const convert = {}
  for (const key of ['t2l', 'pandoc', 'sv2v', 'typst', 'nu']) {
    const value = stringOr(convertInput[key], '')
    if (value) convert[key] = value
  }
  return {
    pythonExecutable: stringOr(input.pythonExecutable, DEFAULTS.pythonExecutable),
    sourceRoot: stringOr(input.sourceRoot, DEFAULTS.sourceRoot),
    bridgePath: stringOr(input.bridgePath, DEFAULTS.bridgePath),
    stateFile: stringOr(input.stateFile, DEFAULTS.stateFile),
    eventsDb: stringOr(input.eventsDb, DEFAULTS.eventsDb),
    activityMode: activityModeOr(input.activityMode, DEFAULTS.activityMode),
    activityRetentionDays: intInRangeOr(
      input.activityRetentionDays,
      DEFAULTS.activityRetentionDays,
      1,
      3650,
    ),
    activityMaxEvents: intInRangeOr(
      input.activityMaxEvents,
      DEFAULTS.activityMaxEvents,
      50,
      1_000_000,
    ),
    commitHistoryKeep: intInRangeOr(
      input.commitHistoryKeep,
      DEFAULTS.commitHistoryKeep,
      1,
      50,
    ),
    commitHistoryMaxFiles: intInRangeOr(
      input.commitHistoryMaxFiles,
      DEFAULTS.commitHistoryMaxFiles,
      10,
      10_000,
    ),
    commitHistoryMaxBytes: intInRangeOr(
      input.commitHistoryMaxBytes,
      DEFAULTS.commitHistoryMaxBytes,
      1024 * 1024,
      2 * 1024 * 1024 * 1024,
    ),
    staleDraftDays: intInRangeOr(
      input.staleDraftDays,
      DEFAULTS.staleDraftDays,
      1,
      3650,
    ),
    workspaceRoots: Array.isArray(input.workspaceRoots)
      ? input.workspaceRoots.filter((entry) => typeof entry === 'string' && entry.trim())
      : DEFAULTS.workspaceRoots,
    convert,
    requireStableRoundTrip: input.requireStableRoundTrip === true,
    requirePreview: booleanOr(input.requirePreview, DEFAULTS.requirePreview),
    promptSectionOrder: finiteNumberOr(
      input.promptSectionOrder,
      DEFAULTS.promptSectionOrder,
    ),
  }
}

export function apply(rawCtx, rawConfig) {
  const config = normaliseConfig(rawConfig)
  const ctx = rawCtx

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

/** Drop separators, and on Windows trailing dots/spaces, that the filesystem ignores. */
function stripTrailingNoise(value) {
  let out = value
  if (process.platform === 'win32') {
    while (out.length > 3 && /[\\/.\s]$/.test(out)) out = out.slice(0, -1)
  } else {
    while (out.length > 1 && out.endsWith('/')) out = out.slice(0, -1)
  }
  return out
}

/**
 * The platform's final path for `value`, resolving the nearest existing ancestor.
 *
 * A target that does not exist yet is the normal case here (`ros_create_artifact`
 * opens a session before anything is written), so a plain `realpathSync` would
 * throw exactly when the fence matters most.
 */
function realpathOrAncestor(value) {
  let current = value
  const tail = []
  for (let depth = 0; depth < 64; depth += 1) {
    try {
      const real = realpathSync.native(current)
      return tail.length ? join(real, ...tail.reverse()) : real
    } catch {
      const parent = dirname(current)
      if (parent === current) return value
      tail.push(basename(current))
      current = parent
    }
  }
  return value
}

/**
 * Canonical key for a path, so the write fence cannot be walked around.
 *
 * `resolve()` alone is not sufficient. On Windows it leaves an 8.3 short name
 * (`PROGRA~1`), a trailing dot or space, and a trailing separator intact, and it
 * does not expand every alias the filesystem accepts. The fence compares keys,
 * so any of those spellings would reach a protected file as a *different* key
 * and slip past the guard. `realpathSync.native` asks the platform for the final
 * path instead, which expands short names and follows links; a path that does not
 * exist yet is canonicalised through its nearest existing ancestor.
 */
function normalisePath(value) {
  if (typeof value !== 'string' || !value.trim()) return ''
  const resolved = stripTrailingNoise(resolve(value))
  const real = stripTrailingNoise(realpathOrAncestor(resolved))
  return process.platform === 'win32' ? real.toLowerCase() : real
}

/** Path inside `root` (or equal to it), compared case-insensitively on Windows. */
function pathWithin(root, candidate) {
  if (!root || !candidate) return false
  if (candidate === root) return true
  const sep = process.platform === 'win32' ? '\\' : '/'
  return candidate.startsWith(root.endsWith(sep) ? root : root + sep)
}

function firstExisting(candidates) {
  for (const candidate of candidates) {
    if (candidate && existsSync(candidate)) return candidate
  }
  return ''
}

function looksLikeVenvPython(root) {
  if (!root) return []
  return process.platform === 'win32'
    ? [join(root, '.verify', 'Scripts', 'python.exe'), join(root, 'Scripts', 'python.exe')]
    : [join(root, '.verify', 'bin', 'python3'), join(root, 'bin', 'python3')]
}

/**
 * The interpreter the shipped `setup.ps1` installs, so a bundle installed from
 * that setup can find its environment even when the profile patch omitted the
 * explicit `pythonExecutable`.
 */
function setupVenvPython() {
  if (process.platform !== 'win32' || !process.env.LOCALAPPDATA) return ''
  return join(process.env.LOCALAPPDATA, 'Rosetta', 'Harness', 'venv', 'Scripts', 'python.exe')
}

/**
 * The Python interpreter that owns the installed `rosetta_studio` package.
 *
 * Configuration wins, then the `ROSETTA_PYTHON` environment variable, then a
 * virtual environment beside the configured source root. There is deliberately
 * no silent fallback to a bare `python`: running the bridge under an interpreter
 * without the package installed produces a confusing ImportError, and a clear
 * refusal with the expected path is more useful.
 */
function resolvePython(config) {
  return firstExisting([
    config.pythonExecutable,
    process.env.ROSETTA_PYTHON || '',
    ...looksLikeVenvPython(config.sourceRoot),
    setupVenvPython(),
    process.platform === 'win32'
      ? join(homedir(), '.rosetta', 'venv', 'Scripts', 'python.exe')
      : join(homedir(), '.rosetta', 'venv', 'bin', 'python3'),
  ])
}

function resolveBridge(config) {
  if (config.bridgePath && existsSync(config.bridgePath)) return config.bridgePath
  if (existsSync(BRIDGE)) return BRIDGE
  return ''
}

function pythonHomeFor(pythonExecutable) {
  // A virtual environment's interpreter lives in <root>/Scripts/python.exe on
  // Windows and <root>/bin/python3 elsewhere; the environment root is the
  // import root that owns the installed package.
  const binDir = dirname(pythonExecutable)
  return dirname(binDir)
}

// ---------------------------------------------------------------------------
// Host half
// ---------------------------------------------------------------------------

  const state = {
    config,
    lastFailure: null,
    disabled: false,
    disableReason: '',
    missingPython: false,
    // The in-flight Rosetta tool call, if any. Only commands tied to a concrete
    // Rosetta session are published; state/diagnostic probes are not activity.
    activity: null,
  }
  let activitySequence = 0

  const protections = new Map() // normalised path -> { session, displayPath, pair }

  /**
   * Fence readiness and ordering.
   *
   * Two failure modes are handled here, and neither was handled before.
   *
   * **Unknown is not the same as empty.** The session list is read
   * asynchronously from the bridge. If that read fails - the bridge cannot
   * start, the store is corrupt, the interpreter is wrong - the fence used to
   * stay *empty*, which reads exactly like "no artifact is managed" and let a
   * native `write`/`edit` straight through a path Rosetta was managing. `ready`
   * records whether a session list has ever been read successfully, so the guard
   * can refuse conservatively instead of guessing.
   *
   * **A stale answer must not win.** Several refreshes overlap (activation, every
   * Rosetta tool call, the post-commit refresh). Each takes a ticket from
   * `sequence`; a result is applied only if nothing newer has already been
   * applied. Without that, an older query returning late would replace the fence
   * with a snapshot from before a session was opened - dropping the protection
   * for a path that is now managed.
   */
  const fence = {
    ready: false,
    sequence: 0,
    applied: 0,
    inFlight: 0,
    lastSuccessAt: null,
    lastError: null,
  }

  function fenceStatus() {
    return {
      ready: fence.ready,
      inFlight: fence.inFlight,
      protectedPaths: protections.size,
      lastSuccessAt: fence.lastSuccessAt,
      lastError: fence.lastError,
      appliedSequence: fence.applied,
    }
  }

  /**
   * True when `rawPath` is inside a configured workspace root.
   *
   * This is the region the plugin is responsible for, and therefore the region
   * in which "I do not know" has to mean "no". Outside it the plugin fences
   * nothing, so refusing would break unrelated work for no safety gain.
   */
  function isInsideConfiguredRoot(rawPath) {
    if (typeof rawPath !== 'string' || !rawPath.trim()) return false
    if (roots.length === 0) return false
    const norm = normalisePath(rawPath)
    if (!norm) return false
    return roots.some((root) => pathWithin(root.norm, norm))
  }

  const python = resolvePython(config)
  const bridge = resolveBridge(config)
  const roots = (config.workspaceRoots || [])
    .filter((root) => typeof root === 'string' && root.trim())
    .map((root) => ({ raw: root.trim(), norm: normalisePath(root.trim()) }))

  /**
   * Add the current chat's working directory when no root was configured.
   *
   * The configured `workspaceRoots` remains the explicit policy and is never
   * widened. Only the zero-config case adopts the Host's own session cwd, so a
   * first open does not require the user to edit YAML. If the Host does not
   * expose a cwd, the plugin stays disabled with the original explanation
   * rather than guessing a process directory.
   */
  function addAutoWorkspaceRoot(rawPath) {
    if (roots.length > 0) return false
    if (typeof rawPath !== 'string' || !rawPath.trim() || !isAbsolute(rawPath)) return false
    const norm = normalisePath(rawPath)
    if (!norm) return false
    roots.push({ raw: rawPath.trim(), norm })
    if (
      state.disabled &&
      !state.missingPython &&
      state.disableReason.startsWith('No `workspaceRoots` configured')
    ) {
      state.disabled = false
      state.disableReason = ''
    }
    return true
  }

  function cwdFromExec(exec) {
    const session = exec && exec.agent && exec.agent.session
    const cwd =
      session && session.header && typeof session.header.cwd === 'string'
        ? session.header.cwd
        : session && typeof session.cwd === 'string'
          ? session.cwd
          : ''
    return cwd
  }

  if (!python || !bridge) {
    state.disabled = true
    state.missingPython = !python
    state.disableReason = !python
      ? 'No Python interpreter with the rosetta_studio package configured ' +
        '(set `pythonExecutable`, or ROSETTA_PYTHON).'
      : `Rosetta bridge script not found at ${BRIDGE}.`
  }
  if (roots.length === 0) {
    state.disabled = true
    state.disableReason =
      'No `workspaceRoots` configured. The plugin refuses to expose any artifact ' +
      'path until at least one allowed root is declared, because the workspace ' +
      'boundary is the plugin\'s main safety property.'
  }

  /**
   * Run one bridge command. The bridge is stateless apart from its JSON store,
   * so every tool call is a fresh short-lived process.
   */
  function callBridge(command, { signal, track = true } = {}) {
    if (state.disabled) {
      return Promise.resolve({
        ok: false,
        error: { code: 'ROSETTA_UNAVAILABLE', message: state.disableReason },
      })
    }
    const tracked =
      track === true &&
      typeof command.session === 'string' &&
      !['state', 'pairs', 'doctor', 'list_pairs', 'status'].includes(command.command)
    const activityTicket = tracked ? ++activitySequence : 0
    if (tracked) {
      state.activity = {
        ticket: activityTicket,
        command: command.command,
        session: command.session,
        startedAt: Date.now(),
      }
    }
    const args = [bridge, '--allow-root', roots[0].raw]
    for (const root of roots.slice(1)) args.push('--allow-root', root.raw)
    args.push('--import-root', pythonHomeFor(python))
    if (config.stateFile) args.push('--store', config.stateFile)
    // Retention limits are passed explicitly so the bridge enforces exactly what
    // this configuration says, and `ros_doctor` reports the accepted values.
    args.push('--activity-mode', config.activityMode)
    args.push('--activity-retention-days', String(config.activityRetentionDays))
    args.push('--activity-max-events', String(config.activityMaxEvents))
    args.push('--history-keep', String(config.commitHistoryKeep))
    args.push('--history-max-files', String(config.commitHistoryMaxFiles))
    args.push('--history-max-bytes', String(config.commitHistoryMaxBytes))
    args.push('--stale-draft-days', String(config.staleDraftDays))
    // The stability gate belongs to the bridge, not to this plugin: only the
    // bridge can refuse *before* the target is written. Checking it here after
    // the call meant a refused delivery had already replaced the file.
    if (config.requireStableRoundTrip) args.push('--require-stable-round-trip')
    // Preview-before-write is enforced, not merely documented. The tool contract
    // already tells the model to preview first; without enforcement a model that
    // skips it rewrites the file the user was never shown.
    if (config.requirePreview) args.push('--require-preview')

    const env = { ...process.env }
    const convert = config.convert || {}
    if (convert.t2l) env.ROSETTA_TYLAX = convert.t2l
    if (convert.pandoc) env.ROSETTA_PANDOC = convert.pandoc
    if (convert.sv2v) env.ROSETTA_SV2V = convert.sv2v
    if (convert.typst) env.ROSETTA_TYPST = convert.typst
    if (convert.nu) env.ROSETTA_NUSHELL = convert.nu
    // Point the bridge at the same activity database the Studio GUI polls.
    if (config.eventsDb) env.ROSETTA_EVENTS_DB = config.eventsDb

    const promise = new Promise((resolvePromise) => {
      let child
      try {
        child = spawn(python, args, { env, windowsHide: true })
      } catch (error) {
        resolvePromise({
          ok: false,
          error: {
            code: 'BRIDGE_SPAWN_FAILED',
            message: `Could not start the Rosetta bridge: ${error.message}`,
          },
        })
        return
      }

      let stdout = ''
      let stderr = ''
      let settled = false
      const finish = (payload) => {
        if (settled) return
        settled = true
        clearTimeout(timer)
        resolvePromise(payload)
      }

      const timer = setTimeout(() => {
        try {
          child.kill()
        } catch {
          /* the process is already gone */
        }
        finish({
          ok: false,
          error: {
            code: 'BRIDGE_TIMEOUT',
            message: `The Rosetta bridge did not answer within ${BRIDGE_TIMEOUT_MS} ms.`,
          },
        })
      }, BRIDGE_TIMEOUT_MS)

      if (signal) {
        signal.addEventListener(
          'abort',
          () => {
            try {
              child.kill()
            } catch {
              /* already gone */
            }
            finish({ ok: false, error: { code: 'ABORTED', message: 'Tool call cancelled.' } })
          },
          { once: true },
        )
      }

      child.stdout.setEncoding('utf8')
      child.stderr.setEncoding('utf8')
      child.stdout.on('data', (chunk) => {
        stdout += chunk
      })
      child.stderr.on('data', (chunk) => {
        stderr += chunk
      })
      child.on('error', (error) => {
        finish({
          ok: false,
          error: {
            code: 'BRIDGE_SPAWN_FAILED',
            message: `Could not run ${python}: ${error.message}`,
          },
        })
      })
      child.on('close', (code) => {
        const lines = stdout.split(/\r?\n/).filter((line) => line.trim())
        if (lines.length === 0) {
          finish({
            ok: false,
            error: {
              code: 'BRIDGE_NO_OUTPUT',
              message:
                `The Rosetta bridge exited with code ${code} and produced no JSON. ` +
                `stderr: ${stderr.trim().slice(0, 600) || '(empty)'}`,
            },
          })
          return
        }
        try {
          const payload = JSON.parse(lines[lines.length - 1])
          if (payload && payload.ok === false) {
            state.lastFailure = payload.error || null
          } else {
            // A recorded failure is a breadcrumb, not a permanent state. Leaving
            // it set made the status route report an error that had long since
            // been fixed, which is worse than reporting nothing.
            state.lastFailure = null
          }
          finish(payload)
        } catch (error) {
          finish({
            ok: false,
            error: {
              code: 'BRIDGE_BAD_JSON',
              message: `The Rosetta bridge returned unparseable output: ${error.message}`,
              stdout: lines[lines.length - 1].slice(0, 400),
            },
          })
        }
      })

      child.stdin.on('error', () => {
        /* the child may exit before reading stdin; the close handler reports it */
      })
      child.stdin.end(JSON.stringify(command))
    })
    if (!tracked) return promise
    return promise.finally(() => {
      if (state.activity && state.activity.ticket === activityTicket) {
        state.activity = null
      }
    })
  }

  /** Turn a bridge refusal into a thrown tool error with a stable code. */
  function unwrap(payload, context) {
    if (payload && payload.ok === true) return payload
    const error = (payload && payload.error) || {
      code: 'BRIDGE_FAILURE',
      message: 'The Rosetta bridge refused the request without a reason.',
    }
    const hint = ACTION_HINTS[error.code]
    const failure = new Error(
      `${error.code}: ${error.message}` + (hint ? `\nWhat to do: ${hint}` : ''),
    )
    failure.rosettaCode = error.code
    failure.rosettaDetails = error.details
    failure.rosettaContext = context
    throw failure
  }

  /**
   * Keep the path fence in step with the store.
   *
   * `write`/`edit` are denied while a target has a live Rosetta session, and the
   * session list lives in the bridge's JSON store, so the fence is refreshed
   * from it. The guard itself stays synchronous, which is what makes the denial
   * hold regardless of listener order.
   */
  async function refreshProtections() {
    if (state.disabled) return
    const token = (fence.sequence += 1)
    fence.inFlight += 1
    let payload = null
    try {
      payload = await callBridge({ command: 'status' })
    } catch (error) {
      payload = { ok: false, error: { message: String((error && error.message) || error) } }
    } finally {
      fence.inFlight -= 1
    }

    // A newer refresh has already been applied: this answer is older than the
    // fence it would replace, so it is dropped rather than allowed to widen it.
    if (token <= fence.applied) return

    if (!payload || payload.ok !== true) {
      // The last known good fence is deliberately kept. Clearing it on failure
      // would *release* protection on paths that may still be managed, which is
      // the unsafe direction; over-fencing a closed session is the safe one.
      fence.lastError =
        (payload && payload.error && payload.error.message) ||
        'the bridge did not answer the status query'
      return
    }

    const live = new Set()
    for (const entry of payload.sessions || []) {
      // A session that can no longer act on its target stops fencing it. The
      // bridge reports this as `active`; a finished session that is not active
      // (committed and then abandoned) must stop blocking the user's own editor,
      // which previously lasted until the record was pruned.
      if (entry.active === false) continue
      const key = normalisePath(entry.targetPath)
      if (!key) continue
      live.add(key)
      const existing = protections.get(key)
      protections.set(key, {
        session: entry.session,
        pair: entry.pair,
        displayPath: entry.targetPath,
        dirty: entry.dirty,
        firstSeen: existing ? existing.firstSeen : Date.now(),
      })
    }
    if (!fence.ready) {
      // First successful read. Before this point the fence was empty because the
      // state was unknown, not because nothing was managed.
      fence.ready = true
    }
    for (const key of [...protections.keys()]) {
      if (!live.has(key)) protections.delete(key)
    }
    fence.applied = token
    fence.lastSuccessAt = Date.now()
    fence.lastError = null
  }

  function forgetProtection(targetPath) {
    protections.delete(normalisePath(targetPath))
  }

  function isProtected(targetPath) {
    const key = normalisePath(targetPath)
    if (!key) return null
    if (protections.has(key)) return protections.get(key)
    // A relative path is resolved against each allowed root before comparison.
    if (!isAbsolute(targetPath)) {
      for (const root of roots) {
        const candidate = normalisePath(join(root.raw, targetPath))
        if (protections.has(candidate)) return protections.get(candidate)
      }
    }
    return null
  }

  // -- path containment, mirrored here so a refusal is immediate -------------

  function requireInsideWorkspace(rawPath) {
    if (typeof rawPath !== 'string' || !rawPath.trim()) {
      throw new Error('PATH_REQUIRED: an absolute artifact path inside a configured workspace root is required.')
    }
    if (roots.length === 0) {
      throw new Error(
        'NO_WORKSPACE_ROOT: no workspace is configured and this Harness chat did not provide an absolute working folder. ' +
          'Run setup.ps1 from the project directory or configure an absolute workspaceRoots path.',
      )
    }
    const candidate = isAbsolute(rawPath) ? resolve(rawPath) : resolve(roots[0].raw, rawPath)
    const norm = normalisePath(candidate)
    if (!roots.some((root) => pathWithin(root.norm, norm))) {
      throw new Error(
        `PATH_OUTSIDE_WORKSPACE: ${candidate} is outside every configured workspace root ` +
          `(${roots.map((r) => r.raw).join(', ')}).`,
      )
    }
    return candidate
  }

  const requirement = { signal: true }

  // -- tool definitions ------------------------------------------------------

  const TOOLS = {
    ros_doctor: {
      description:
        'Report the Rosetta integration: the Python interpreter and installed package, the ' +
        'external converters and prqlc module with their configured paths, which tool pairs ' +
        'each missing converter blocks, the allowed workspace roots, and the accepted ' +
        'activityMode (activity-log mode).',
      parameters: { type: 'object', properties: {}, additionalProperties: false },
      output: { type: 'object' },
      async execute() {
        const payload = await callBridge({ command: 'doctor' })
        if (!payload.ok) {
          return {
            available: false,
            pluginDisabled: state.disabled,
            reason: state.disabled ? state.disableReason : payload.error.message,
            pythonExecutable: python || null,
            bridge: bridge || null,
            workspaceRoots: roots.map((root) => root.raw),
            activityMode: config.activityMode,
          }
        }
        return {
          available: true,
          pythonExecutable: python,
          bridge,
          workspaceRoots: roots.map((root) => root.raw),
          // Report the accepted Host setting explicitly. `retention.activityMode`
          // is the bridge's view of the same activity-log mode; keeping this
          // top-level field avoids making the agent infer configuration from
          // a nested retention summary.
          activityMode: payload.retention?.activityMode || config.activityMode,
          python: payload.python,
          imports: payload.imports,
          converters: payload.converters,
          pairDiagnostics: payload.pairDiagnostics,
          limits: payload.limits,
          fence: payload.fence,
          retention: payload.retention,
          storage: payload.storage,
          // Proof of which Host bundle answered this call. `check_activation`
          // compares the same digest with the installed bundle.
          generation: { digest: GENERATION.digest, files: GENERATION.files },
          tools: Object.keys(TOOLS).sort(),
        }
      },
    },

    ros_maintenance: {
      description:
        'Report or clean the local state Rosetta keeps under <workspace>/.rosetta: activity-log ' +
        'rows, the saved previous versions used by "Restore previous", and finished session ' +
        'records. The default (action "report", apply false) deletes nothing. It separates ' +
        'backups that ros_restore_last can still reach (their session exists) from backups whose ' +
        'session has ended or expired (orphans, unreachable). Orphans are reported but removed ' +
        'only when action is "clean", apply is true, includeOrphanHistory is true, AND their ' +
        'newest delivery is older than olderThanDays. A dirty draft - an undelivered familiar-view ' +
        'edit - is never removed unless action is "clean", apply is true, includeDrafts is true, ' +
        'AND the draft is older than olderThanDays. Pending commit journal entries are never ' +
        'cleaned; they are unfinished deliveries that get replayed. ros_doctor shows the same ' +
        'counts without changing anything.',
      parameters: {
        type: 'object',
        properties: {
          action: {
            type: 'string',
            enum: ['report', 'clean'],
            description: 'report (default) is read-only. clean with apply=true removes what the policy allows.',
          },
          apply: {
            type: 'boolean',
            description: 'For action=clean: false (default) is a dry run; true performs the removal.',
          },
          includeDrafts: {
            type: 'boolean',
            description: 'For action=clean with apply=true: also remove undelivered drafts older than olderThanDays. Default false.',
          },
          includeOrphanHistory: {
            type: 'boolean',
            description: 'For action=clean with apply=true: also remove saved previous versions whose session no longer exists and whose newest delivery is older than olderThanDays. The newest backup of a live session is never removed. Default false.',
          },
          olderThanDays: {
            type: 'integer',
            description: 'Age threshold for abandoned drafts and orphaned backups, in days (1-3650). Defaults to the configured staleDraftDays.',
          },
        },
        additionalProperties: false,
      },
      output: { type: 'object' },
      async execute(args, exec) {
        const chat = requireChatSessionFromExec(exec, 'Rosetta maintenance')
        const payload = unwrap(
          await callBridge({
            command: 'maintenance',
            action: args.action || 'report',
            apply: args.apply === true,
            include_drafts: args.includeDrafts === true,
            include_orphan_history: args.includeOrphanHistory === true,
            older_than_days: args.olderThanDays,
            harness_session: chat,
          }),
          'maintenance',
        )
        return payload
      },
    },

    ros_list_pairs: {
      description:
        'List the Rosetta tool pairs: the target (new) format the artifact is written in, ' +
        'the familiar (old) view the agent edits, and whether the pair is experimental. ' +
        'Call this only when the target extension is ambiguous or the user asks which pairs are supported. ' +
        'For a known target extension (.typ / .sv / .prql / .nu), open it directly; the pair is inferred.',
      parameters: { type: 'object', properties: {}, additionalProperties: false },
      output: { type: 'object' },
      async execute() {
        const payload = unwrap(await callBridge({ command: 'pairs' }), 'list_pairs')
        return { pairs: payload.pairs, converters: payload.converters }
      },
    },

    ros_open_artifact: {
      description:
        'Open an EXISTING target artifact and return the familiar view to work in. ' +
        '`pair` is optional and is inferred from the target extension ' +
        '(.typ / .sv / .prql / .nu); pass it explicitly only to override that inference. ' +
        '`targetPath` must be inside a configured workspace root. The returned `agentView` is ' +
        'the only view the agent should edit, and `viewId`/`session` identifies this session ' +
        'for every later call. Reading the target file directly with the plain file tools ' +
        'still shows the target format, not this view.',
      parameters: {
        type: 'object',
        properties: {
          pair: { type: 'string', description: `Optional pair name; inferred from the extension when omitted. Known names: ${PAIR_CHOICES.map((p) => JSON.stringify(p)).join(', ')}.` },
          targetPath: { type: 'string', description: 'Target artifact path inside an allowed workspace root. Prefer an absolute path; a workspace-relative path is also accepted and resolved against the current workspace.' },
          session: { type: 'string', description: 'Optional stable id for this session; reuse it across turns so the pending view is not lost. Defaults to a new id.' },
        },
        required: ['targetPath'],
        additionalProperties: false,
      },
      output: { type: 'object' },
      async execute(args, exec) {
        const chat = requireChatSessionFromExec(exec, 'Opening an artifact')
        addAutoWorkspaceRoot(cwdFromExec(exec))
        requireInsideWorkspace(args.targetPath)
        const pair = args.pair || inferPair(args.targetPath)
        if (!pair) {
          throw new Error(
            `UNKNOWN_PAIR: cannot infer a Rosetta pair from ${args.targetPath}. ` +
              `Use one of ${PAIR_CHOICES.map((p) => JSON.stringify(p)).join(', ')} or a ` +
              `target extension .typ / .sv / .prql / .nu.` +
              `\nWhat to do: ${ACTION_HINTS.UNKNOWN_PAIR}`,
          )
        }
        const session = args.session || `ros-${randomUUID()}`
        const payload = unwrap(
          await callBridge({
            command: 'open',
            session,
            pair,
            path: args.targetPath,
            // The Host's own Session identity, used only for status-strip
            // ownership. It never becomes part of the artifact path policy.
            harness_session: chat,
          }),
          'open',
        )
        await refreshProtections()
        return {
          session: payload.session,
          pair: payload.pair,
          targetPath: payload.targetPath,
          viewExtension: payload.viewExtension,
          targetExtension: payload.targetExtension,
          agentView: payload.agentView,
          characters: payload.characters,
          ownerSessionId: payload.ownerSessionId || null,
          notes: payload.notes || [],
        }
      },
    },

    ros_create_artifact: {
      description:
        'Start a Rosetta session for a NEW target artifact. Prefer `initialAgentView` in the familiar ' +
        'language so Rosetta can translate it to the target; `initialTargetContent` is a legacy ' +
        'option for callers that already have target-language text. Nothing is written to disk until ' +
        'the user reviews the candidate and clicks Deliver in the Harness status strip; ' +
        'ros_commit_delivery only asks the user to do that and cannot write the file. The ' +
        'familiar view is held in the session. `pair` is optional and is inferred from the target extension ' +
        '(.typ / .sv / .prql / .nu). The parent directory must already exist and the target must ' +
        'not exist. To start a new file with target-native pass-through blocks, create that target file natively and then open it with Rosetta.',
      parameters: {
        type: 'object',
        properties: {
          pair: { type: 'string', description: 'Optional pair name; inferred from the extension when omitted.' },
          targetPath: { type: 'string', description: 'Path for the new artifact inside an allowed workspace root. Prefer an absolute path; a workspace-relative path is also accepted and resolved against the current workspace. The target must not exist yet.' },
          initialAgentView: { type: 'string', description: 'Preferred: initial content in the familiar agent-view language (LaTeX, Verilog, SQL, or Bash). Rosetta translates it into the target format during preview. Do not include pass-through markers when creating a new artifact. May be empty.' },
          initialTargetContent: { type: 'string', description: 'Legacy option: initial content already written in the target format (Typst, SystemVerilog, PRQL, or Nushell), which Rosetta projects into the familiar view. Prefer initialAgentView instead.' },
          session: { type: 'string', description: 'Optional stable session id.' },
        },
        required: ['targetPath'],
        additionalProperties: false,
      },
      output: { type: 'object' },
      async execute(args, exec) {
        const chat = requireChatSessionFromExec(exec, 'Creating an artifact')
        addAutoWorkspaceRoot(cwdFromExec(exec))
        requireInsideWorkspace(args.targetPath)
        if (args.initialAgentView !== undefined && args.initialTargetContent !== undefined) {
          throw new Error(
            `CREATE_CONTENT_AMBIGUOUS: provide initialAgentView or initialTargetContent, not both.\nWhat to do: ${ACTION_HINTS.CREATE_CONTENT_AMBIGUOUS}`,
          )
        }
        const pair = args.pair || inferPair(args.targetPath)
        if (!pair) {
          throw new Error(
            `UNKNOWN_PAIR: cannot infer a Rosetta pair from ${args.targetPath}. ` +
              `Use one of ${PAIR_CHOICES.map((p) => JSON.stringify(p)).join(', ')} or a ` +
              `target extension .typ / .sv / .prql / .nu.` +
              `\nWhat to do: ${ACTION_HINTS.UNKNOWN_PAIR}`,
          )
        }
        const session = args.session || `ros-${randomUUID()}`
        const fromAgentView = args.initialTargetContent === undefined
        const payload = unwrap(
          await callBridge({
            command: fromAgentView ? 'create_from_view' : 'create',
            session,
            pair,
            path: args.targetPath,
            ...(fromAgentView
              ? { agent_view: args.initialAgentView ?? '' }
              : { content: args.initialTargetContent }),
            harness_session: chat,
          }),
          fromAgentView ? 'create_from_view' : 'create',
        )
        await refreshProtections()
        return {
          session: payload.session,
          pair: payload.pair,
          targetPath: payload.targetPath,
          viewExtension: payload.viewExtension,
          targetExtension: payload.targetExtension,
          agentView: payload.agentView,
          characters: payload.characters,
          ownerSessionId: payload.ownerSessionId || null,
          notes: payload.notes || [],
          written: false,
        }
      },
    },

    ros_agent_view: {
      description:
        'Read or write the familiar view of an open Rosetta session. ' +
        'action "read" returns the current view; action "write" replaces it with `content` ' +
        '(the whole view, in the familiar format). Writing only updates the session: the target ' +
        'file on disk is untouched until the user clicks Deliver in the Harness status strip; ' +
        'ros_commit_delivery is only a request and cannot write the file.',
      parameters: {
        type: 'object',
        properties: {
          action: { type: 'string', enum: ['read', 'write'], description: 'Read the view, or replace it.' },
          session: { type: 'string', description: 'The session id returned by ros_open_artifact or ros_create_artifact.' },
          content: { type: 'string', description: 'Required for action "write": the complete new view in the familiar format.' },
        },
        required: ['action', 'session'],
        additionalProperties: false,
      },
      output: { type: 'object' },
      async execute(args, exec) {
        const chat = requireChatSessionFromExec(exec, 'Reading or editing a familiar view')
        if (args.action === 'read') {
          const payload = unwrap(
            await callBridge({
              command: 'read',
              session: args.session,
              harness_session: chat,
            }),
            'read',
          )
          return {
            session: payload.session,
            pair: payload.pair,
            targetPath: payload.targetPath,
            agentView: payload.agentView,
            dirty: payload.dirty,
          }
        }
        if (typeof args.content !== 'string') {
          throw new Error(
            'CONTENT_REQUIRED: action "write" needs `content`.' +
              `\nWhat to do: ${ACTION_HINTS.CONTENT_REQUIRED}`,
          )
        }
        const payload = unwrap(
          await callBridge({
            command: 'write',
            session: args.session,
            content: args.content,
            harness_session: chat,
          }),
          'write',
        )
        await refreshProtections()
        return {
          session: payload.session,
          stored: true,
          characters: payload.characters,
          dirty: payload.dirty,
        }
      },
    },

    ros_preview_delivery: {
      description:
        'Translate the current view back to the TARGET format and show it, WITHOUT writing any ' +
        'file. Always call this and read the result before ros_commit_delivery. The result also ' +
        'reports whether the target changed on disk since the session opened, and whether the ' +
        'delivery would project back to the current view (the round-trip check). This is a ' +
        'candidate preview: it does not verify that the requested change was fulfilled, and a ' +
        'pass-through-only view means nothing was translated.',
      parameters: {
        type: 'object',
        properties: {
          session: { type: 'string', description: 'The Rosetta session id.' },
        },
        required: ['session'],
        additionalProperties: false,
      },
      output: { type: 'object' },
      async execute(args, exec) {
        const chat = requireChatSessionFromExec(exec, 'Previewing a delivery')
        const payload = unwrap(
          await callBridge({
            command: 'preview',
            session: args.session,
            harness_session: chat,
          }),
          'preview',
        )
        return {
          session: payload.session,
          pair: payload.pair,
          targetPath: payload.targetPath,
          targetContent: payload.targetContent,
          changedFromDelivered: payload.changedFromDelivered,
          targetDrift: payload.targetDrift,
          roundTrip: payload.roundTrip,
          dirty: payload.dirty,
          // A preview is evidence about text, not about whether the requested
          // change was expressed. These fields keep the two claims separate in
          // both the tool result and the status strip.
          candidateOnly: true,
          requestVerified: false,
          hasConvertibleContent: payload.hasConvertibleContent !== false,
          viewEdited: payload.viewEdited === true,
          conversion: payload.conversion || null,
          ownerSessionId: payload.ownerSessionId || null,
          notes: payload.notes || [],
        }
      },
    },

    ros_commit_delivery: {
      description:
        'Request delivery of a previewed candidate. This tool no longer writes the file: ' +
        'the user must review the candidate in the Harness status strip and click Deliver. ' +
        'If the user has not done that yet, show them the preview and ask them to confirm ' +
        'in the UI. The actual write is performed only by that user click, bound to the ' +
        'preview, view and target digests the user saw.',
      parameters: {
        type: 'object',
        properties: {
          session: { type: 'string', description: 'The Rosetta session id.' },
        },
        required: ['session'],
        additionalProperties: false,
      },
      output: { type: 'object' },
      async execute(_args, exec) {
        requireChatSessionFromExec(exec, 'Delivering an artifact')
        throw new Error(
          'USER_CONFIRMATION_REQUIRED: delivery is performed only when the user clicks ' +
            'Deliver in the Harness status strip, after reviewing the previewed candidate. ' +
            'Run ros_preview_delivery, show the candidate, and ask the user to click ' +
            'Deliver. A model-supplied confirmation flag is not accepted.',
        )
      },
    },

    ros_close_session: {
      description:
        'Discard a Rosetta session without writing the target. Use it to abandon an edit, or to ' +
        'release the path fence so the plain file tools may modify the target again.',
      parameters: {
        type: 'object',
        properties: {
          session: { type: 'string', description: 'The Rosetta session id to discard.' },
        },
        required: ['session'],
        additionalProperties: false,
      },
      output: { type: 'object' },
      async execute(args, exec) {
        const chat = requireChatSessionFromExec(exec, 'Closing a session')
        const payload = unwrap(
          await callBridge({
            command: 'close',
            session: args.session,
            harness_session: chat,
          }),
          'close',
        )
        await refreshProtections()
        return {
          session: payload.session,
          closed: payload.closed,
          discardedPendingEdits: payload.discardedPendingEdits,
        }
      },
    },

    ros_harness_status: {
      description:
        'Report every live Rosetta session: the target artifact, the pair, whether the view has ' +
        'unsaved edits, whether the target changed on disk, and whether a delivery preview is ' +
        'pending. Use it to see what the user is looking at in the Harness status strip.',
      parameters: {
        type: 'object',
        properties: {
          session: { type: 'string', description: 'Optional: report only this session.' },
        },
        additionalProperties: false,
      },
      output: { type: 'object' },
      async execute(args, exec) {
        const chat = requireChatSessionFromExec(exec, 'Listing Rosetta sessions')
        const payload = unwrap(
          await callBridge({
            command: 'state',
            session: args.session || null,
            harness_session: chat,
          }),
          'state',
        )
        return {
          count: payload.count,
          sessions: (payload.sessions || []).map((entry) => ({
            session: entry.session,
            pair: entry.pair,
            targetPath: entry.targetPath,
            dirty: entry.dirty,
            hasPreview: entry.hasPreview,
            previewCandidate: entry.previewCandidate,
            hasConvertibleContent: entry.hasConvertibleContent,
            viewEdited: entry.viewEdited,
            requestVerified: entry.requestVerified,
            conversion: entry.conversion,
            ownerSessionId: entry.ownerSessionId || null,
            targetDrift: entry.targetDrift,
            // `active` is the live write fence, not "the draft still exists".
            // The status strip shows the same flag, so the agent can explain an
            // idle session without claiming its unsaved work was discarded.
            active: entry.active,
            restoreAvailable: entry.restoreAvailable,
            targetPresent: entry.targetPresent,
            updatedAt: entry.updatedAt,
            projectionMetadata: entry.projectionMetadata,
          })),
          // The fence's own health. `ready: false` means native writes inside a
          // configured root are being refused conservatively, and `lastError`
          // says why.
          fence: fenceStatus(),
        }
      },
    },
  }

  ctx.effect(() => {
    const disposers = []
    for (const [toolName, definition] of Object.entries(TOOLS)) {
      disposers.push(
        ctx.tools.register({
          name: toolName,
          description: definition.description,
          parameters: definition.parameters,
          output: {
            schema: definition.output,
            render(_args, value) {
              // A throwing render would blank the whole tool result, so the
              // worst case degrades to the raw JSON instead of losing the call.
              try {
                return [{ type: 'text', text: renderToolResult(toolName, value) }]
              } catch (error) {
                return [
                  {
                    type: 'text',
                    text:
                      `Rosetta: could not format this result (${error && error.message}). ` +
                      `Raw value:\n${JSON.stringify(value)}`,
                  },
                ]
              }
            },
          },
          execute: (args, exec) => definition.execute(args, exec),
        }),
      )
    }
    return () => {
      for (const dispose of disposers) dispose()
    }
  })

  // -- the path fence --------------------------------------------------------

  ctx.effect(() =>
    ctx.tools.guard((execution) => {
      if (state.disabled) return undefined
      if (!MUTATING_TOOLS.has(execution.name)) return undefined
      const args = execution.arguments
      if (!args || typeof args !== 'object') return undefined
      const target = args.file_path
      if (typeof target !== 'string' || !target) return undefined
      const protection = isProtected(target)
      if (protection) {
        return (
          `Rosetta is managing ${protection.displayPath} as an open ` +
          `"${protection.pair}" session (${protection.session}), so ${execution.name} is denied on ` +
          `this exact path. Edit the familiar view with ros_agent_view, then run ` +
          `ros_preview_delivery and ask the user to click Deliver in the status strip, ` +
          `or call ros_close_session to release the fence first.`
        )
      }

      // The session list has never been read, so "not in the fence" does not
      // mean "not managed" - it means the question is unanswered. Inside the
      // configured roots the answer has to be no: this is exactly the window in
      // which a managed artifact would otherwise be writable directly.
      if (!fence.ready && isInsideConfiguredRoot(target)) {
        const reason =
          fence.lastError ||
          'the first status query has not completed yet'
        return (
          `Rosetta cannot currently tell whether ${target} is managed: it has not been able ` +
          `to read its session list (${reason}). Because ${target} is inside a configured ` +
          `workspace root, ${execution.name} is denied conservatively rather than risk writing ` +
          `straight over an artifact a Rosetta session may own. This is not a verdict about this ` +
          `path - it is a refusal to guess.\n` +
          `To resolve it: run ros_doctor to see why the bridge is unavailable, or fix the session ` +
          `store it reported. Paths outside the configured roots are unaffected.`
        )
      }
      return undefined
    }),
  )

  /**
   * Seed the fence at activation.
   *
   * Until this resolves, `fence.ready` is false and the guard refuses writes
   * inside the configured roots - so the denial holds from the first moment the
   * plugin is applied, rather than only after a successful read. A failure is
   * retried with backoff so a transient problem heals without a restart; a
   * persistent one keeps refusing, which is the intended conservative outcome.
   */
  let fenceRetryTimer = null
  function seedFence(attempt = 0) {
    refreshProtections()
      .then(() => {
        if (fence.ready || state.disabled) return
        const delay = Math.min(30000, 500 * 2 ** attempt)
        fenceRetryTimer = setTimeout(() => seedFence(attempt + 1), delay)
        if (fenceRetryTimer.unref) fenceRetryTimer.unref()
      })
      .catch(() => {
        const delay = Math.min(30000, 500 * 2 ** attempt)
        fenceRetryTimer = setTimeout(() => seedFence(attempt + 1), delay)
        if (fenceRetryTimer.unref) fenceRetryTimer.unref()
      })
  }
  ctx.effect(() => {
    seedFence(0)
    // A session can become idle without another tool call. Refresh the host's
    // synchronous guard so an abandoned draft does not hold a path indefinitely.
    const interval = setInterval(() => { void refreshProtections() }, 60_000)
    if (interval.unref) interval.unref()
    return () => {
      if (fenceRetryTimer) clearTimeout(fenceRetryTimer)
      clearInterval(interval)
    }
  })

  // -- status surface for the Client half ------------------------------------

  /**
   * Is the caller allowed to reach this route?
   *
   * An unauthenticated route on the browser carrier is readable by any local
   * process. Measured before this check existed: `GET /rosetta-harness/state`
   * answered `200` with artifact text to a request that carried no headers, a
   * foreign `Origin`, a bogus bearer token and a bogus cookie - while the
   * Harness's own `/`, `/api` and `/api/workspaces` all answered `401`. The
   * Host already owns the trust decision, so the route asks it rather than
   * inventing a second scheme: `ctx.connection.requestRejection` applies the
   * same Host/Origin checks and browser authentication to another Web route.
   *
   * Fails closed. If the connection service is unavailable, or the check itself
   * throws, the answer is a refusal - a status strip is not worth an
   * unauthenticated content channel.
   */
  function rejectUnauthenticated(req) {
    const connection = ctx.get('connection')
    if (!connection || typeof connection.requestRejection !== 'function') {
      return { status: 503, reason: 'the connection service is unavailable, so this route cannot authenticate callers' }
    }
    try {
      const rejection = connection.requestRejection({ headers: req.headers })
      if (rejection) return { status: rejection, reason: null }
      return null
    } catch (error) {
      return { status: 503, reason: `the trust check failed: ${error && error.message}` }
    }
  }

  /**
   * Build the headers every state-route response carries.
   *
   * `x-rosetta-generation` is the digest of the three files this process
   * loaded. It is a hash and a short tool-name list, never artifact text, and it
   * is returned even on a 401 so a local operator script can distinguish "the
   * new module is live" from "an older module is still answering 401". The
   * header is advisory for the browser; the actual authorisation result is
   * unchanged.
   */
  function generationHeaders(extra = {}) {
    return {
      'x-rosetta-generation': GENERATION.digest || 'unavailable',
      'x-rosetta-tools': Object.keys(TOOLS).sort().join(','),
      ...extra,
    }
  }

  /**
   * Build the status-strip snapshot.
   *
   * When the Client can name its current chat (`?chat=<sessionId>`), sessions
   * owned by another chat are withheld and only their count is reported. The
   * query value is a view selector, not an authorisation: the action route
   * separately requires the same chat id and the bridge verifies it against the
   * owner recorded at open/create. A missing or unattributed owner stays
   * unattributed; it is never filled in from the caller.
   */
  // Pair availability is expensive to discover (it imports every adapter and
  // probes the external binaries). The status strip does not need it on every
  // 2.5s poll, so cache it briefly and surface it with the state snapshot.
  let pairStatusCache = null
  let pairStatusAt = 0
  let pairStatusError = null

  async function readPairStatus(force = false) {
    if (!force && pairStatusAt && Date.now() - pairStatusAt < 60_000) {
      return pairStatusCache
    }
    const payload = await callBridge({ command: 'pairs' })
    if (!payload.ok) {
      pairStatusError = payload.error.message
      pairStatusAt = Date.now()
      return null
    }
    const pairs = (payload.pairs || []).map((pair) => {
      const backend = pair.backend || {}
      const missingRequired = Array.isArray(backend.missing_required)
        ? backend.missing_required
        : []
      const missingOptional = Array.isArray(backend.missing_optional)
        ? backend.missing_optional
        : []
      return {
        name: pair.name,
        targetExtension: pair.targetExtension,
        viewExtension: pair.viewExtension,
        experimental: pair.experimental === true,
        available: missingRequired.length === 0,
        missingRequired,
        missingOptional,
        hint: backend.hint || null,
      }
    })
    pairStatusCache = {
      pairs,
      converters: payload.converters || null,
      checkedAt: new Date().toISOString(),
    }
    pairStatusAt = Date.now()
    pairStatusError = null
    return pairStatusCache
  }

  const snapshot = async ({ includeContent = false, chatSessionId = '', full = false } = {}) => {
    const scoped = normaliseChatSessionId(chatSessionId)
    // A strip without a stable chat id may show metadata, but must never ask
    // the bridge to load artifact bodies. When a chat is known, the bridge
    // applies this owner filter before returning any content to the Host.
    includeContent = includeContent && Boolean(scoped)
    const base = {
      workspaceRoots: roots.map((root) => root.raw),
      chatScoped: Boolean(scoped),
      otherSessionCount: 0,
      pairDiagnosticsError: pairStatusError,
      activity: null,
      // Identifies the Host module answering this poll. The strip can show the
      // short form, and check_activation compares the long form with the bundle.
      generation: { digest: GENERATION.digest, files: GENERATION.files },
    }
    if (state.disabled) {
      return {
        ...base,
        ok: false,
        disabled: true,
        reason: state.disableReason,
        fence: fenceStatus(),
        pairs: null,
        sessions: [],
      }
    }
    const [payload, pairInfo] = await Promise.all([
      callBridge({
        command: 'state',
        include_content: includeContent,
        view_limit: full ? 4_000_000 : 4000,
        ...(includeContent ? { content_owner_session: scoped } : {}),
      }),
      readPairStatus(),
    ])
    if (!payload.ok) {
      return {
        ...base,
        ok: false,
        disabled: false,
        reason: payload.error.message,
        fence: fenceStatus(),
        pairs: pairInfo ? pairInfo.pairs : null,
        sessions: [],
      }
    }
    const all = payload.sessions || []
    const sessions = scoped
      ? all.filter((entry) => entry.ownerSessionId === scoped)
      : all
    const activity =
      state.activity && sessions.some((entry) => entry.session === state.activity.session)
        ? {
            command: state.activity.command,
            session: state.activity.session,
            startedAt: state.activity.startedAt,
          }
        : null
    return {
      ...base,
      ok: true,
      lastFailure: state.lastFailure,
      contentIncluded: Boolean(payload.contentIncluded),
      otherSessionCount: scoped ? all.length - sessions.length : 0,
      pairs: pairInfo ? pairInfo.pairs : null,
      pairDiagnosticsError: pairStatusError,
      activity,
      fence: fenceStatus(),
      sessions,
    }
  }

  // -- model guidance --------------------------------------------------------
  //
  // Registering tools is not enough to get them used. Nothing in a tool's own
  // description can tell the model that these tools are *the* way to edit a
  // Typst or SystemVerilog file, so a model asked to "fix this .typ file" reaches
  // for the built-in write tool and is then refused by the fence - which reads as
  // the plugin being broken. This section states the decision, the sequence and
  // the constraints that are not derivable from the individual tool schemas.
  //
  // The service is optional: `ctx.get` keeps activation working on a Harness build
  // without `systemPrompt`.
  const GUIDANCE = [
    'Some files in this workspace are written in a format the user does not want to',
    'edit directly: Typst (`.typ`), SystemVerilog (`.sv`), PRQL (`.prql`) or Nushell',
    '(`.nu`). Rosetta projects such a file into a familiar view, keeps the target',
    'format on disk, and writes changes back only on an explicit commit.',
    '',
    'When the user asks to read, edit, fix or extend one of those files, use the',
    '`ros_*` tools: `ros_open_artifact` (or `ros_create_artifact` for a new file),',
    'For a new file, author the starting content in the familiar language and pass it as',
    '`initialAgentView` to `ros_create_artifact`; do not write target-language content',
    'unless the user supplied it or explicitly requested native authoring.',
    'Pass the known target path directly to the Rosetta tool. Prefer an absolute',
    'path; a workspace-relative path is also accepted and resolved against the',
    'current chat workspace in zero-config mode. Do not search the workspace to',
    'locate a file whose path the user already provided. If the',
    'path is unclear or outside the allowed root, ask the user to select the file',
    'or workspace. Never widen or bypass the workspace boundary.',
    'When the file extension identifies a pair, omit `pair` and let the tool infer',
    'it. Call `ros_list_pairs` only when the extension is ambiguous or the user asks',
    'which pairs are available.',
    'edit the returned view, and run `ros_preview_delivery`. The user must review the',
    'candidate in the Harness status strip and click Deliver; `ros_commit_delivery`',
    'does not write and is not a confirmation mechanism. Leave the session available',
    'for review after preview or delivery. `ros_close_session` discards the session and',
    'can remove its restore affordance; call it only when the user explicitly asks to',
    'discard or release that session. Do not edit those files with the built-in `write`',
    'or `edit`; while a session is open on a path the built-in tools are denied by design.',
    'A `round trip: exact` result means only that re-projecting the generated target',
    'reproduced the submitted familiar view byte for byte. It does not prove that the',
    'requested change was made, that the target compiles, or that its behavior is correct.',
    'Do not summarize it as semantic or functional correctness; report compiler checks',
    'separately when the tool result provides them.',
    'Do not call `ros_doctor` before every routine edit; use it when the user asks',
    'about setup or when a Rosetta tool reports an availability or converter error.',
    '',
    'Pass-through blocks in the view carry target-only content that cannot be',
    'translated. Leave them byte for byte; removing or reordering them is refused.',
    '',
    'Session state can change after earlier messages. Never infer that a draft,',
    'preview, or open session is still pending from conversation history. Before',
    'describing or recommending an action for an existing session, call',
    '`ros_harness_status` and use its current-chat result; if it lists no session,',
    'report only that no live session or actionable draft is visible. Do not infer',
    'whether an old draft was delivered, discarded, or expired; its historical',
    'outcome is unknown unless a terminal tool result or verified target file proves',
    'it. Do not suggest delivering or restoring an old draft. The status strip is',
    'the user-visible source of truth. When handling a different artifact, do not',
    'interrupt the task with unrelated sessions; mention another session only if it',
    'is for the same target or the user asks about it.',
    '',
    'An idle session releases the Harness write/edit fence after about 30 minutes,',
    'but its undelivered draft stays in the local session store: idle does not mean',
    'discarded. `ros_doctor` reports the fence and retention limits, and',
    '`ros_maintenance` reports or cleans local state (a dry run by default).',
  ].join('\n')

  ctx.effect(() => {
    const systemPrompt = ctx.get('systemPrompt')
    if (!systemPrompt || typeof systemPrompt.section !== 'function') return () => {}
    // A missing workspace root is precisely the zero-configuration case: the
    // first Rosetta open derives it from the current Harness chat. Suppressing
    // guidance while that root is not known means the model is never told to
    // make that first open. Only omit the section when the runtime itself is
    // unavailable; workspace discovery is deferred until the tool call.
    if (!python || !bridge) return () => {}
    try {
      return systemPrompt.section({
        name: 'rosetta-compat',
        order: config.promptSectionOrder,
        text: GUIDANCE,
      })
    } catch {
      // The guidance is an affordance, never a precondition: losing it must not
      // take the tools down with it.
      return () => {}
    }
  })

  ctx.effect(() =>
    ctx.webServer.register({
      kind: 'exact',
      path: '/rosetta-harness/state',
      async handler(req, res) {
        if (req.method !== 'GET' && req.method !== 'HEAD') {
          res.writeHead(405, generationHeaders({ 'content-type': 'application/json' }))
          res.end(JSON.stringify({ ok: false, error: { code: 'METHOD_NOT_ALLOWED' } }))
          return
        }

        const denied = rejectUnauthenticated(req)
        if (denied) {
          // No body on a refusal: an unauthenticated caller learns nothing, not
          // even whether a session exists. The generation header is a build
          // identity, not session data; check_activation.ps1 needs it precisely
          // because it cannot authenticate.
          res.writeHead(denied.status, generationHeaders({ 'cache-control': 'no-store' }))
          res.end()
          return
        }

        // Artifact bodies are a separate, explicit request so the ambient strip
        // cannot become a content feed. `?content=1` is still authenticated.
        let includeContent = false
        let chatSessionId = ''
        let full = false
        try {
          const url = new URL(req.url || '/', 'http://localhost')
          includeContent = url.searchParams.get('content') === '1'
          full = url.searchParams.get('full') === '1'
          chatSessionId = normaliseChatSessionId(url.searchParams.get('chat'))
        } catch {
          includeContent = false
          full = false
          chatSessionId = ''
        }

        let body
        try {
          body = await snapshot({ includeContent, chatSessionId, full })
        } catch (error) {
          body = {
            ok: false,
            reason: String(error && error.message),
            chatScoped: Boolean(chatSessionId),
            otherSessionCount: 0,
            generation: { digest: GENERATION.digest, files: GENERATION.files },
            sessions: [],
          }
        }
        const text = JSON.stringify(body)
        // Missing workspace configuration is a usable setup state for the
        // status strip, not an unavailable HTTP service.
        res.writeHead(200, generationHeaders({
          'content-type': 'application/json; charset=utf-8',
          'cache-control': 'no-store',
          'content-length': Buffer.byteLength(text),
        }))
        res.end(req.method === 'HEAD' ? undefined : text)
      },
    }),
  )

  // The browser's Preview, Deliver and Release buttons use the same bridge
  // commands and the same Host authentication as the agent tools. No artifact
  // body is accepted from the browser; edits stay in the existing session.
  ctx.effect(() =>
    ctx.webServer.register({
      kind: 'exact',
      path: '/rosetta-harness/action',
      async handler(req, res) {
        const reply = (status, body) => {
          const data = JSON.stringify(body)
          res.writeHead(status, {
            'content-type': 'application/json; charset=utf-8',
            'cache-control': 'no-store',
            'content-length': Buffer.byteLength(data),
          })
          res.end(data)
        }
        if (req.method !== 'POST') {
          reply(405, { ok: false, error: 'Use POST.' })
          return
        }
        const denied = rejectUnauthenticated(req)
        if (denied) {
          res.writeHead(denied.status, { 'cache-control': 'no-store' })
          res.end()
          return
        }
        if (!String(req.headers['content-type'] || '').toLowerCase().startsWith('application/json')) {
          reply(415, { ok: false, error: 'Expected application/json.' })
          return
        }
        let raw = ''
        try {
          for await (const chunk of req) {
            raw += chunk.toString()
            if (raw.length > 4096) {
              reply(413, { ok: false, error: 'Request is too large.' })
              return
            }
          }
          const body = JSON.parse(raw)
          const action = body && body.action
          if (
            !body ||
            ![
              'preview',
              'commit',
              'close',
              'open',
              'release-other',
              'discard-preview',
              'restore-last',
            ].includes(action)
          ) {
            reply(400, {
              ok: false,
              error:
                'Choose preview, commit, close, open, discard-preview, restore-last or release-other.',
            })
            return
          }
          if (action === 'open') {
            if (typeof body.targetPath !== 'string' || !body.targetPath ||
                body.targetPath.length > 2048 ||
                typeof body.pair !== 'string' || !body.pair || body.pair.length > 200) {
              reply(400, {
                ok: false,
                error: 'Opening a file in this chat needs targetPath and pair.',
              })
              return
            }
          } else if (typeof body.session !== 'string' || !body.session || body.session.length > 200) {
            reply(400, { ok: false, error: 'Choose preview, commit or close for a session.' })
            return
          }
          if (action === 'commit') {
            for (const key of [
              'expectedPreviewSha256',
              'expectedViewSha256',
              'expectedTargetSha256',
            ]) {
              const value = body[key]
              if (value === undefined || value === null) continue
              if (typeof value !== 'string' || !/^[0-9a-f]{64}$/i.test(value)) {
                reply(400, {
                  ok: false,
                  error: `A confirmed commit needs a valid ${key} (or null for no target).`,
                })
                return
              }
            }
          }
          if (action === 'release-other' && body.confirmDiscard !== true) {
            reply(400, {
              ok: false,
              error: 'Releasing another chat\'s session requires confirmDiscard: true.',
            })
            return
          }
          // A browser action must name the chat the strip is rendering. Without
          // it the Host cannot tell a stale preview left by another chat from
          // this chat's work, and the safe answer is to perform nothing.
          const harnessSessionId = normaliseChatSessionId(body.harnessSessionId)
          if (!harnessSessionId) {
            reply(400, {
              ok: false,
              error:
                'CHAT_SESSION_REQUIRED: this status strip could not identify the current chat, ' +
                'so browser Preview/Deliver/Release are disabled. Use the Rosetta tools in the chat.',
            })
            return
          }
          if (action === 'open') {
            // Opening from the "+N other" panel creates a new session owned by
            // the current chat. The old session is left untouched; its unsaved
            // draft is not transferred, and its actions stay locked.
            if (roots.length === 0) {
              const sessions = ctx.get && ctx.get('sessions')
              if (sessions && typeof sessions.list === 'function') {
                try {
                  const current = sessions
                    .list()
                    .find(
                      (entry) =>
                        (entry.id || (entry.header && entry.header.id)) === harnessSessionId,
                    )
                  addAutoWorkspaceRoot(
                    current && current.header ? current.header.cwd : current && current.cwd,
                  )
                } catch {
                  // The session service is optional; the bridge reports the
                  // missing-root refusal below when it is unavailable.
                }
              }
            }
            const session = `ros-${randomUUID()}`
            const payload = unwrap(
              await callBridge({
                command: 'open',
                session,
                pair: body.pair,
                path: body.targetPath,
                harness_session: harnessSessionId,
              }),
              'open',
            )
            await refreshProtections()
            reply(200, {
              ok: true,
              action: 'open',
              session: payload.session,
              pair: payload.pair,
              targetPath: payload.targetPath,
              opened: true,
            })
            return
          }
          if (action === 'discard-preview') {
            const payload = unwrap(
              await callBridge({
                command: 'discard_preview',
                session: body.session,
                harness_session: harnessSessionId,
              }),
              'discard_preview',
            )
            await refreshProtections()
            reply(200, {
              ok: true,
              action: 'discard-preview',
              session: payload.session,
              discarded: payload.discarded === true,
            })
            return
          }
          if (action === 'restore-last') {
            const payload = unwrap(
              await callBridge({
                command: 'restore_last',
                session: body.session,
                harness_session: harnessSessionId,
              }),
              'restore_last',
            )
            await refreshProtections()
            reply(200, {
              ok: true,
              action: 'restore-last',
              session: payload.session,
              restored: payload.restored === true,
              targetPath: payload.targetPath,
            })
            return
          }
          if (action === 'release-other') {
            // An explicit, confirmed cleanup from the read-only other-session
            // panel. It skips the ownership check only for this browser action;
            // the agent's ros_close_session still enforces ownership.
            const payload = unwrap(
              await callBridge({
                command: 'close',
                session: body.session,
                harness_session: harnessSessionId,
                release_foreign: true,
              }),
              'close',
            )
            await refreshProtections()
            reply(200, {
              ok: true,
              action: 'release-other',
              session: body.session,
              discardedPendingEdits: payload.discardedPendingEdits === true,
            })
            return
          }
          const payload = unwrap(
            await callBridge({
              command: action,
              session: body.session,
              harness_session: harnessSessionId,
              ...(action === 'commit'
                ? {
                    expected_preview_sha256: body.expectedPreviewSha256 ?? null,
                    expected_view_sha256: body.expectedViewSha256 ?? null,
                    expected_target_sha256: body.expectedTargetSha256 ?? null,
                  }
                : {}),
            }),
            action,
          )
          await refreshProtections()
          reply(200, {
            ok: true,
            action,
            session: body.session,
            // Preview content is read through the authenticated state route.
            written: payload.written === true,
            discardedPendingEdits: payload.discardedPendingEdits === true,
          })
        } catch (error) {
          reply(error.rosettaCode ? 409 : 500, {
            ok: false,
            error: String((error && error.message) || error),
          })
        }
      },
    }),
  )
}

// ---------------------------------------------------------------------------
// Tool result rendering
// ---------------------------------------------------------------------------

/**
 * One readable line about what the round-trip check actually proved.
 *
 * The raw status is `exact` / `unstable` / `unknown`. "unstable" on its own reads
 * as a failure. A text mismatch can be formatting or a semantic change, and
 * the bridge cannot prove which. Report the measured comparison precisely.
 */
function roundTripLine(roundTrip) {
  const status = roundTrip.status || 'unknown'
  if (status === 'exact') {
    return (
      'round trip: exact - the delivery re-projects to the view byte for byte. ' +
      'This checks text preservation only; it does not verify that the requested change was made.'
    )
  }
  if (status === 'unknown') {
    return `round trip: not verified${roundTrip.reason ? ` - ${roundTrip.reason}` : ''}`
  }
  if (roundTrip.preservedRegionsExact === true && roundTrip.translatedByteIdentical === true) {
    return (
      'round trip: protected blocks and translated text match; marker placement ' +
      'or blank lines differ. Review the delivery.'
    )
  }
  if (roundTrip.preservedRegionsExact === false) {
    return 'round trip: a protected block changed - review the delivery before relying on it.'
  }
  if (roundTrip.formattingDifference === 'blank_line_after_latex_heading') {
    return (
      'round trip: only a blank line after a LaTeX heading differs. ' +
      'The text check remains inconclusive; review the target preview before delivery.'
    )
  }
  return 'round trip: the re-projected text differs from the view; this check cannot ' +
    'tell formatting changes from content changes. Review the delivery.'
}

function fenced(label, text) {
  return `${label}\n\`\`\`\n${text}\n\`\`\``
}

function renderToolResult(toolName, value) {
  if (!value || typeof value !== 'object') return JSON.stringify(value)

  if (toolName === 'ros_list_pairs') {
    const lines = (value.pairs || []).map((pair) => {
      const flags = [pair.experimental ? 'experimental' : 'stable']
      if (pair.backend && pair.backend.missing_required) {
        flags.push(`UNAVAILABLE: needs ${pair.backend.missing_required.join(', ')}`)
      } else if (pair.backend && pair.backend.missing_optional) {
        flags.push(`degraded: ${pair.backend.missing_optional.join(', ')} not found`)
      }
      return `- ${pair.name}  target ${pair.targetExtension} / view ${pair.viewExtension}  [${flags.join('; ')}]`
    })
    return ['Rosetta tool pairs:', ...lines].join('\n')
  }

  if (toolName === 'ros_doctor') {
    if (value.available === false) {
      return `Rosetta is not available.\nReason: ${value.reason}\npython: ${value.pythonExecutable}\nbridge: ${value.bridge}`
    }
    const converters = Object.entries(value.converters || {}).map(
      ([name, info]) =>
        `- ${name}: ${info.found ? info.path : `NOT FOUND (set ${info.env_var})`}`,
    )
    const blocked = (value.pairDiagnostics || []).map(
      (entry) => `- ${entry.pair}: ${entry.hint}`,
    )
    const fence = value.fence || {}
    const retention = value.retention || {}
    const storage = value.storage || {}
    const sessions = storage.sessions || {}
    const history = storage.commitHistory || {}
    const mib = (bytes) => `${Math.round(Number(bytes || 0) / (1024 * 1024))} MiB`
    return [
      `Rosetta bridge: OK`,
      `python: ${value.pythonExecutable}`,
      `package: ${(value.imports || {}).rosetta_studio || '(unknown)'}`,
      `host bundle generation: ${value.generation?.digest ? String(value.generation.digest).slice(0, 16) + '…' : '(not reported)'}`,
      `workspace roots: ${(value.workspaceRoots || []).join(', ')}`,
      '',
      'External converters:',
      ...converters,
      ...(blocked.length ? ['', 'Pair diagnostics:', ...blocked] : []),
      '',
      'Write fence (who may modify a managed target):',
      `- harness write/edit: blocked while a session is active; any session releases the fence after ${fence.idleMinutes ?? 30} min without activity`,
      '- released is not discarded: an undelivered draft stays in the session store, and delivery still refuses when the target changed on disk',
      '- shell commands, other plugins and external editors are never fenced; Rosetta reports their writes as target drift',
      '',
      'Local retention (what Rosetta keeps under .rosetta):',
      `- activityMode: ${value.activityMode ?? retention.activityMode ?? 'metadata'} (activity-log mode; full stores text snapshots, off disables activity rows); retention ${retention.activityRetentionDays ?? 30} days / ${retention.activityMaxEvents ?? 5000} rows; only rows written by this plugin are pruned`,
      `- saved previous versions: newest ${retention.commitHistoryKeepPerSession ?? 3} per session, ceiling ${retention.commitHistoryMaxFiles ?? 200} files / ${mib(retention.commitHistoryMaxBytes)}`,
      `- sessions: ${sessions.total ?? 0} stored, ${sessions.dirty ?? 0} with an undelivered draft (never deleted automatically)`,
      `- history on disk: ${history.entries ?? 0} entr${history.entries === 1 ? 'y' : 'ies'}, ${mib(history.bytes)}; activity db ${mib((storage.activityLog || {}).bytes)}`,
      '- ros_maintenance reports the same counts and is a dry run by default',
    ].join('\n')
  }

  if (toolName === 'ros_maintenance') {
    const activity = value.activity || {}
    const history = value.commitHistory || {}
    const sessions = value.sessions || {}
    const drafts = value.drafts || {}
    const mode = value.applied ? 'APPLIED' : 'dry run - nothing removed'
    return [
      `Rosetta local state: ${mode}`,
      `activity log: ${activity.mode} mode, ${activity.rows ?? 0} row(s); ` +
        `${value.applied ? `removed ${activity.deleted ?? 0}` : `would remove ${activity.wouldDelete ?? 0}`} ` +
        `(only rows written by this plugin)`,
      `saved previous versions: ${history.entries ?? 0} entr${history.entries === 1 ? 'y' : 'ies'}, ${history.bytes ?? 0} bytes; ` +
        `${value.applied ? `removed ${history.removedFiles ?? 0} file(s)` : `would remove ${history.wouldRemove ?? 0} superseded entr${history.wouldRemove === 1 ? 'y' : 'ies'}`}`,
      `undo reachable now: ${history.liveSessionsWithHistory ?? 0} live session(s); ` +
        `orphaned by ended/expired sessions: ${history.orphanSessions ?? 0} session(s), ` +
        `${history.orphanEntries ?? 0} entr${history.orphanEntries === 1 ? 'y' : 'ies'} (${history.orphanBytes ?? 0} bytes)` +
        (history.includeOrphanHistory
          ? `; ${value.applied ? `removed ${history.removedOrphanEntries ?? 0} entr${history.removedOrphanEntries === 1 ? 'y' : 'ies'}` : `would remove ${history.wouldRemoveOrphans ?? 0} entr${history.wouldRemoveOrphans === 1 ? 'y' : 'ies'}`}`
          : '; kept (pass includeOrphanHistory=true to include them in an applied clean)'),
      ...(history.examples || []).map(
        (entry) => `  - orphan ${entry.session}  ${entry.targetPath}  idle ${entry.idleDays} day(s), ${entry.entries} entr${entry.entries === 1 ? 'y' : 'ies'}`,
      ),
      `sessions: ${sessions.total ?? 0} stored, ${sessions.dirty ?? 0} with an undelivered draft; ` +
        `${value.applied ? 'removed' : 'would remove'} ${sessions.removableClean ?? 0} finished record(s)`,
      `drafts older than ${drafts.olderThanDays ?? value.olderThanDays} day(s): ${drafts.stale ?? 0}` +
        (drafts.includeDrafts
          ? `; ${value.applied ? `removed ${drafts.removed ?? 0}` : `would remove ${drafts.wouldRemove ?? 0}`}`
          : '; kept (pass includeDrafts=true to include them in an applied clean)'),
      ...(drafts.examples || []).map(
        (entry) => `  - ${entry.session}  ${entry.targetPath}  idle ${entry.idleDays} day(s)`,
      ),
      `commit journal: ${(value.journal || {}).pending ?? 0} pending (never cleaned)`,
      ...(value.notes || []).map((note) => `note: ${note}`),
    ].join('\n')
  }

  if (toolName === 'ros_open_artifact' || toolName === 'ros_create_artifact') {
    const head = [
      `Session ${value.session}  pair ${value.pair}`,
      `target: ${value.targetPath}`,
      `view extension: ${value.viewExtension}   target extension: ${value.targetExtension}`,
      ...(value.notes || []).map((note) => `note: ${note}`),
    ]
    return [head.join('\n'), '', fenced('Familiar view (edit this):', value.agentView || '')].join('\n')
  }

  if (toolName === 'ros_agent_view') {
    if (value.stored) {
      return `View stored for session ${value.session} (${value.characters} characters, dirty: ${value.dirty}). The target file is unchanged.`
    }
    return [
      `Session ${value.session}  pair ${value.pair}`,
      `target: ${value.targetPath}  (unsaved edits: ${value.dirty})`,
      '',
      fenced('Familiar view:', value.agentView || ''),
    ].join('\n')
  }

  if (toolName === 'ros_preview_delivery') {
    const drift = value.targetDrift || {}
    const roundTrip = value.roundTrip || {}
    const convertible = value.hasConvertibleContent !== false
    const edited = value.viewEdited === true
    const editLine = !convertible
      ? 'convertible content: NONE - the familiar view is pass-through only; nothing was translated'
      : edited
        ? 'view edit: the familiar view changed, so this is a candidate delivery for that edit; Rosetta cannot determine whether the request was fulfilled'
        : 'view edit: NONE - the familiar view is unchanged since it was opened, so this preview contains no edit'
    return [
      `Candidate delivery preview for ${value.targetPath} (nothing written)`,
      'requested change check: not performed - Rosetta checks text and round-trip shape, not task intent',
      editLine,
      `target changed on disk since the session opened: ${drift.drifted ? `YES - ${drift.reason}` : 'no'}`,
      roundTripLine(roundTrip),
      ...(value.notes || []).map((note) => `note: ${note}`),
      '',
      fenced('Would be written (target format):', value.targetContent || ''),
    ].join('\n')
  }

  if (toolName === 'ros_commit_delivery') {
    const verification = value.verification || {}
    let checkLine
    if (verification.checked) {
      const scope = verification.independent === false
        ? 'same-tool parse check, NOT an independent second opinion'
        : 'independent check'
      checkLine =
        `${scope} with ${verification.tool}: ` +
        (verification.ok ? 'PASSED' : `FAILED - ${verification.stderr || verification.error}`)
    } else {
      checkLine = `independent check: not run (${verification.reason || 'n/a'})`
    }
    const caveat = value.hasConvertibleContent === false
      ? 'No convertible content was translated: the target was written from pass-through text carried back. This is not evidence that the requested change was made.'
      : value.viewEdited === false
        ? 'The familiar view was unchanged since it was opened, so no edit was delivered.'
        : null
    return [
      `WROTE ${value.targetPath} (${value.bytes} bytes, sha256 ${String(value.targetSha256).slice(0, 16)}...)`,
      ...(caveat ? [caveat] : []),
      roundTripLine(value.roundTrip || {}),
      checkLine,
      ...(verification.note ? [`note: ${verification.note}`] : []),
      ...(value.notes || []).map((note) => `note: ${note}`),
    ].join('\n')
  }

  if (toolName === 'ros_close_session') {
    return `Session ${value.session} closed.${value.discardedPendingEdits ? ' Pending edits were discarded.' : ''}`
  }

  if (toolName === 'ros_harness_status') {
    if (!value.sessions || value.sessions.length === 0) return 'No live Rosetta sessions.'
    return [
      `${value.count} live Rosetta session(s):`,
      ...value.sessions.map(
        (entry) =>
          `- ${entry.session}  ${entry.pair}\n  target: ${entry.targetPath}` +
          `\n  owner chat: ${entry.ownerSessionId || '(unknown - not attributed)'}` +
          `\n  unsaved edits: ${entry.dirty}   preview pending: ${entry.hasPreview}` +
          `\n  write fence: ${
            entry.active === false
              ? 'released after inactivity - the draft is still stored in the session store, and delivery still checks the target hash'
              : 'active (harness write/edit is denied on this path)'
          }` +
          `\n  previous version saved: ${entry.restoreAvailable ? 'yes' : 'no'}` +
          `\n  convertible content: ${entry.hasConvertibleContent === false ? 'none (pass-through only)' : 'present'}` +
          `\n  target changed on disk: ${(entry.targetDrift || {}).drifted ? 'YES' : 'no'}`,
      ),
    ].join('\n')
  }

  return JSON.stringify(value, null, 1)
}

/**
 * Host-half harness test for the Rosetta Harness plugin.
 *
 * Loads `index.js` exactly as the Harness does (`apply(ctx, config)`) against a
 * minimal fake Cordis context, then drives every registered Rosetta tool and the
 * `write`/`edit` guard. This exercises the plugin's own logic — tool schemas,
 * result rendering, path policy, the confirmation gate, and the path fence —
 * without needing the profile to reload.
 *
 * Run:
 *   node tests/host_harness_test.mjs <dsh-workspace-dir>
 */
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import { createHash, randomUUID } from 'node:crypto'
import { tmpdir } from 'node:os'
import { basename, dirname, isAbsolute, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { Readable } from 'node:stream'

const HERE = dirname(fileURLToPath(import.meta.url))
const PLUGIN = resolve(HERE, '..', 'index.js')

// The digest the Host computes at load time, recomputed here from the same
// three files so the generation header, the status payload and ros_doctor can
// be compared with an independent value.
const pluginRoot = dirname(PLUGIN)
const expectedGeneration = createHash('sha256')
  .update(
    [
      ['index.js', join(pluginRoot, 'index.js')],
      ['client.js', join(pluginRoot, 'client.js')],
      ['python/rosetta_harness_bridge.py', join(pluginRoot, 'python', 'rosetta_harness_bridge.py')],
    ]
      .map(([name, path]) => `${name}:${createHash('sha256').update(readFileSync(path)).digest('hex')}`)
      .join('\n') + '\n',
    'utf8',
  )
  .digest('hex')

const results = []
function check(label, condition, detail = '') {
  results.push({ label, ok: Boolean(condition), detail })
  console.log(`[${condition ? 'PASS' : 'FAIL'}] ${label}${condition || !detail ? '' : ` :: ${detail}`}`)
}

// --- a minimal Cordis-shaped context --------------------------------------

function makeContext({ trustCheck, systemPrompt } = {}) {
  const tools = new Map()
  const guards = []
  const routes = new Map()
  const effects = []
  const trustCalls = []
  const promptSections = []

  const ctx = {
    tools: {
      register(definition) {
        if (!definition || typeof definition.name !== 'string') {
          throw new Error('register() needs a definition with a name')
        }
        if (tools.has(definition.name)) throw new Error(`duplicate tool ${definition.name}`)
        tools.set(definition.name, definition)
        return () => tools.delete(definition.name)
      },
      guard(guard) {
        guards.push(guard)
        return () => {
          const index = guards.indexOf(guard)
          if (index >= 0) guards.splice(index, 1)
        }
      },
    },
    webServer: {
      register(route) {
        if (routes.has(route.path)) throw new Error(`duplicate route ${route.path}`)
        routes.set(route.path, route)
        return () => routes.delete(route.path)
      },
    },
    // The Host's own trust decision, as `ctx.connection.requestRejection`.
    get(name) {
      if (name === 'systemPrompt') return systemPrompt
      if (name !== 'connection') return undefined
      if (!trustCheck) return undefined
      return {
        requestRejection(request) {
          trustCalls.push(request)
          return trustCheck(request)
        },
      }
    },
    effect(callback) {
      const disposer = callback()
      effects.push(disposer)
      return disposer
    },
  }
  return { ctx, tools, guards, routes, effects, trustCalls, promptSections }
}

const DEFAULT_CHAT = 'chat-A'

async function call(tools, name, args, exec = {}) {
  const definition = tools.get(name)
  if (!definition) throw new Error(`tool ${name} is not registered`)
  return definition.execute(args, {
    signal: new AbortController().signal,
    // The real Host supplies `exec.agent.session.id`; keep a stable fake here
    // so ownership is exercised rather than assumed.
    agent: { session: { id: DEFAULT_CHAT } },
    ...exec,
  })
}

function render(tools, name, value) {
  const definition = tools.get(name)
  const blocks = definition.output.render({}, value)
  if (!Array.isArray(blocks) || blocks[0]?.type !== 'text') {
    throw new Error('render() must return text content blocks')
  }
  return blocks[0].text
}

// --- run ------------------------------------------------------------------

const workspaceBase = resolve(process.argv[2] || join(HERE, '..', 'demo_workspace'))
mkdirSync(workspaceBase, { recursive: true })
// Keep the generated path short enough for Windows APIs without long-path
// support; the full run UUID on the parent workspace already provides isolation.
const workspace = join(workspaceBase, `h-${randomUUID()}`)
mkdirSync(workspace)
// The bridge keeps its session store inside a workspace root, by design: the
// workspace boundary is the plugin's main safety property, so the state file
// lives under it too.
const stateDir = join(workspace, '_harness_test_state')
rmSync(stateDir, { recursive: true, force: true })
mkdirSync(stateDir, { recursive: true })

const module = await import(`file:///${PLUGIN.replace(/\\/g, '/')}`)
const { ctx, tools, guards, routes } = makeContext({
  // Deny callers that present no Host token, exactly as the real connection
  // service does for the Harness's own /api routes.
  trustCheck(request) {
    const headers = request && request.headers ? request.headers : {}
    const token = headers['x-dsh-token'] || headers['authorization']
    return token === 'valid-token' ? undefined : 401
  },
})

const config = {
  pythonExecutable: process.env.ROSETTA_TEST_PYTHON || '',
  workspaceRoots: [workspace],
  stateFile: join(stateDir, 'sessions.sqlite3'),
  convert: {
    t2l: process.env.ROSETTA_TYLAX || '',
    pandoc: process.env.ROSETTA_PANDOC || '',
    sv2v: process.env.ROSETTA_SV2V || '',
  },
}

module.apply(ctx, config)

// --- registration ---------------------------------------------------------

const expectedTools = [
  'ros_doctor',
  'ros_list_pairs',
  'ros_open_artifact',
  'ros_create_artifact',
  'ros_agent_view',
  'ros_preview_delivery',
  'ros_commit_delivery',
  'ros_close_session',
  'ros_harness_status',
  'ros_maintenance',
]
check(
  'registers every Rosetta tool',
  expectedTools.every((name) => tools.has(name)),
  `missing: ${expectedTools.filter((name) => !tools.has(name)).join(', ')}`,
)
check('registers exactly one path fence', guards.length === 1, `guards=${guards.length}`)
check(
  'registers the status route',
  routes.has('/rosetta-harness/state'),
  [...routes.keys()].join(', '),
)
check('registers the action route', routes.has('/rosetta-harness/action'))

for (const name of expectedTools) {
  const definition = tools.get(name)
  if (!definition) continue
  const params = definition.parameters
  check(
    `${name}: native JSON Schema parameters`,
    params && params.type === 'object' && params.properties && typeof params.description !== 'string',
    JSON.stringify(params).slice(0, 200),
  )
  check(
    `${name}: declares an output schema and renderer`,
    definition.output && definition.output.schema && typeof definition.output.render === 'function',
  )
}

// --- diagnostics ----------------------------------------------------------

const doctor = await call(tools, 'ros_doctor', {})
check('ros_doctor reports availability', doctor.available === true, JSON.stringify(doctor).slice(0, 400))
check(
  'ros_doctor names the installed package path',
  typeof (doctor.imports || {}).rosetta_studio === 'string',
  JSON.stringify(doctor.imports || {}).slice(0, 200),
)
check(
  'ros_doctor reports converter status',
  typeof (doctor.converters || {}).sv2v === 'object',
)
check(
  'ros_doctor publishes the Host generation and tool surface',
  typeof doctor.generation?.digest === 'string'
    && doctor.generation.digest === expectedGeneration
    && Array.isArray(doctor.tools)
    && doctor.tools.includes('ros_maintenance'),
  JSON.stringify({ generation: doctor.generation, tools: doctor.tools }).slice(0, 300),
)
// The two facts the strip, the doctor text and the README must agree on: the
// write fence is released on idle, and that is not the same as a discarded
// draft. The payload is what the renderer and the agent both read.
check(
  'ros_doctor separates the idle fence window from draft retention',
  Number(doctor.fence?.idleSeconds) > 0
    && String(doctor.fence?.note || '').includes('draft stays')
    && String(doctor.retention?.note || '').includes('never removes an undelivered draft'),
  JSON.stringify({ fence: doctor.fence, retention: doctor.retention }).slice(0, 400),
)
check(
  'ros_doctor reports local storage counts',
  doctor.storage
    && typeof (doctor.storage.sessions || {}).dirty === 'number'
    && typeof (doctor.storage.commitHistory || {}).entries === 'number'
    && typeof (doctor.storage.activityLog || {}).mode === 'string',
  JSON.stringify(doctor.storage || {}).slice(0, 400),
)
check(
  'ros_doctor exposes activityMode explicitly and matches bridge retention',
  doctor.activityMode === doctor.retention?.activityMode,
  JSON.stringify({ activityMode: doctor.activityMode, retention: doctor.retention }).slice(0, 400),
)
const doctorText = render(tools, 'ros_doctor', doctor)
check(
  'ros_doctor text says an idle release keeps the draft',
  doctorText.includes('released is not discarded')
    && doctorText.includes('never deleted automatically'),
  doctorText.slice(0, 600),
)
check(
  'ros_doctor text labels metadata as the activityMode',
  doctorText.includes(`activityMode: ${doctor.activityMode}`)
    && doctorText.includes('activity-log mode'),
  doctorText.slice(0, 700),
)
console.log('        doctor text:\n' + render(tools, 'ros_doctor', doctor).split('\n').map((l) => '        ' + l).join('\n'))

// Maintenance defaults to a read-only report. The dialog-free path matters:
// an agent asked to "clean up" must not delete a draft merely by calling the
// tool without the explicit flags.
{
  const report = await call(tools, 'ros_maintenance', {})
  check(
    'ros_maintenance defaults to a dry-run report',
    report.action === 'report'
      && report.applied === false
      && report.drafts?.includeDrafts === false
      && report.journal
      && typeof report.journal.pending === 'number',
    JSON.stringify(report).slice(0, 500),
  )
  const text = render(tools, 'ros_maintenance', report)
  check(
    'ros_maintenance text says nothing was removed and that drafts are kept',
    text.includes('dry run - nothing removed')
      && text.includes('kept (pass includeDrafts=true'),
    text.slice(0, 600),
  )
}

// Invalid configuration must degrade to a usable plugin, not disable it. The
// doctor payload publishes the values that were actually accepted, so a typo is
// visible instead of silently changing retention.
{
  const invalidStateDir = join(workspace, '_harness_test_state_invalid')
  rmSync(invalidStateDir, { recursive: true, force: true })
  mkdirSync(invalidStateDir, { recursive: true })
  const invalid = makeContext({})
  module.apply(invalid.ctx, {
    ...config,
    stateFile: join(invalidStateDir, 'sessions.sqlite3'),
    activityMode: 'loud',
    activityRetentionDays: 'soon',
    activityMaxEvents: 10_000_000,
    commitHistoryKeep: 0,
  })
  const normalized = await call(invalid.tools, 'ros_doctor', {})
  check(
    'invalid retention config falls back or clamps instead of disabling the plugin',
    normalized.available === true
      && normalized.activityMode === 'metadata'
      && normalized.retention?.activityMode === 'metadata'
      && normalized.retention?.activityRetentionDays === 30
      && normalized.retention?.activityMaxEvents === 1_000_000
      && normalized.retention?.commitHistoryKeepPerSession === 1,
    JSON.stringify(normalized.retention || {}).slice(0, 400),
  )
  rmSync(invalidStateDir, { recursive: true, force: true })
}

const pairs = await call(tools, 'ros_list_pairs', {})
check('ros_list_pairs returns the four pairs', (pairs.pairs || []).length === 4, JSON.stringify(pairs.pairs || []).slice(0, 200))
check(
  'ros_list_pairs guidance does not force an unnecessary call for known extensions',
  tools.get('ros_list_pairs').description.includes('Call this only when the target extension is ambiguous')
    && tools.get('ros_list_pairs').description.includes('the pair is inferred'),
  tools.get('ros_list_pairs').description,
)

// Opening a file should not force the caller to name its pair when the
// extension already determines it.
{
  const inferredPath = join(workspace, 'infer-pair.typ')
  writeFileSync(inferredPath, '= Inferred pair\n\nBody.\n', 'utf8')
  const inferred = await call(tools, 'ros_open_artifact', {
    targetPath: inferredPath,
    session: 'infer-pair',
  })
  check(
    'ros_open_artifact infers the pair from the target extension',
    inferred.pair === 'LaTeX / Typst' && inferred.viewExtension === '.tex',
    JSON.stringify(inferred).slice(0, 300),
  )
  await call(tools, 'ros_close_session', { session: 'infer-pair' })
  rmSync(inferredPath, { force: true })
}

// Generated IDs must stay distinct even when a caller opens multiple files in
// one turn without supplying an explicit session value.
{
  const target = join(workspace, 'generated-session-id.prql')
  writeFileSync(target, 'from employees\nselect name\n', 'utf8')
  const [first, second] = await Promise.all([
    call(tools, 'ros_open_artifact', { targetPath: target }),
    call(tools, 'ros_open_artifact', { targetPath: target }),
  ])
  check(
    'generated session ids are unique for concurrent opens',
    typeof first.session === 'string'
      && typeof second.session === 'string'
      && first.session !== second.session
      && first.session.startsWith('ros-')
      && second.session.startsWith('ros-'),
    JSON.stringify([first.session, second.session]),
  )
  await Promise.all([
    call(tools, 'ros_close_session', { session: first.session }),
    call(tools, 'ros_close_session', { session: second.session }),
  ])
  rmSync(target, { force: true })
}

// The preview result is a candidate, not a verification of the requested edit.
{
  const passthrough = render(tools, 'ros_preview_delivery', {
    targetPath: join(workspace, 'request_summary.nu'),
    targetContent: 'open data/requests.csv\n| where status == "open"\n| get owner\n| uniq\n| length\n',
    targetDrift: { drifted: false },
    roundTrip: { status: 'exact' },
    hasConvertibleContent: false,
    viewEdited: false,
    notes: [],
  })
  check(
    'preview render calls a pass-through result a candidate, not a success',
    passthrough.includes('Candidate delivery preview')
      && passthrough.includes('requested change check: not performed')
      && passthrough.includes('convertible content: NONE')
      && !passthrough.includes('requested modification verified: NO')
      && !passthrough.includes('preview ready'),
    passthrough.slice(0, 500),
  )
  const edited = render(tools, 'ros_preview_delivery', {
    targetPath: join(workspace, 'query.prql'),
    targetContent: 'from employees\nfilter age > 30\nselect {name, age}',
    targetDrift: { drifted: false },
    roundTrip: { status: 'unstable' },
    hasConvertibleContent: true,
    viewEdited: true,
    notes: [],
  })
  check(
    'preview render never claims an edited candidate was verified',
    edited.includes('Candidate delivery preview')
      && edited.includes('requested change check: not performed')
      && edited.includes('Rosetta cannot determine whether the request was fulfilled')
      && !edited.includes('requested modification verified: NO'),
    edited.slice(0, 500),
  )
}

// --- the main loop --------------------------------------------------------

// Exercise the real JS-to-Python bridge with a view large enough to expose
// pipe truncation and JSON transport limits; keep the fixture isolated.
const largeTarget = join(workspace, 'large-host.typ')
const largeSource = '= Operations\n\n' + Array.from({ length: 6000 }, (_, i) =>
  `== Section ${i}\n\nRecord ${i}: verify the service and recovery procedure.`).join('\n\n') + '\n'
writeFileSync(largeTarget, largeSource, 'utf8')
const largeOpened = await call(tools, 'ros_open_artifact', {
  pair: 'LaTeX / Typst', targetPath: largeTarget, session: 'large-host-session',
})
check('large target crosses the JS bridge intact', typeof largeOpened.agentView === 'string'
  && largeOpened.agentView.includes('Record 0:') && largeOpened.agentView.includes('Record 5999:'))
if (typeof largeOpened.agentView === 'string') {
  const largeEdit = largeOpened.agentView.replace('Record 5999: verify', 'Record 5999: inspect')
  const largeWritten = await call(tools, 'ros_agent_view', { action: 'write', session: 'large-host-session', content: largeEdit })
  check('large view is stored through the JS bridge', largeWritten.stored === true && largeEdit !== largeOpened.agentView)
  const largePreview = await call(tools, 'ros_preview_delivery', { session: 'large-host-session' })
  check('large preview contains the edit and leaves target untouched',
    typeof largePreview.targetContent === 'string' && largePreview.targetContent.includes('Record 5999: inspect')
      && readFileSync(largeTarget, 'utf8') === largeSource)
  const largeResponse = { status: null, body: '' }
  await routes.get('/rosetta-harness/state').handler(
    { method: 'GET', url: `/rosetta-harness/state?content=1&full=1&chat=${encodeURIComponent(DEFAULT_CHAT)}`,
      headers: { 'x-dsh-token': 'valid-token' } },
    { writeHead(status) { largeResponse.status = status }, end(body) { largeResponse.body = body || '' } },
  )
  const largeState = JSON.parse(largeResponse.body)
  const largeEntry = (largeState.sessions || []).find((entry) => entry.session === 'large-host-session')
  check('full state exposes both complete large target and preview', largeResponse.status === 200
    && largeState.contentIncluded === true && largeEntry?.targetPreview === largeSource
    && largeEntry?.preview === largePreview.targetContent)
  await call(tools, 'ros_close_session', { session: 'large-host-session' })
}

const target = join(workspace, 'harness-loop.typ')
writeFileSync(
  target,
  [
    '= Harness loop',
    '',
    'The compatibility layer gives an agent a familiar LaTeX view.',
    '',
    '#show heading: set text(fill: blue)',
    '',
  ].join('\n'),
  'utf8',
)
const originalTarget = readFileSync(target, 'utf8')

const opened = await call(tools, 'ros_open_artifact', {
  pair: 'LaTeX / Typst',
  targetPath: target,
  session: 'harness-session',
})
check('ros_open_artifact returns a LaTeX view', opened.agentView.includes('\\documentclass'))
check(
  'ros_open_artifact marks the Typst-only directive',
  opened.agentView.includes('@TYPST-PASSTHROUGH') && opened.agentView.includes('#show heading'),
)
check(
  'ros_open_artifact surfaces the preservation note',
  (opened.notes || []).some((note) => note.includes('new-format-only')),
  JSON.stringify(opened.notes || []),
)

// The fence must now deny the plain write tool on the exact target.
const guard = guards[0]
const denial = guard({ name: 'write', arguments: { file_path: target } })
check('fence denies `write` on the open target', typeof denial === 'string' && denial.includes('ros_agent_view'), String(denial))
const editDenial = guard({ name: 'edit', arguments: { file_path: target } })
check('fence denies `edit` on the open target', typeof editDenial === 'string')
const otherPath = guard({ name: 'write', arguments: { file_path: join(workspace, 'unrelated.txt') } })
check('fence allows `write` on an unrelated path', otherPath === undefined, String(otherPath))
const unrelatedTool = guard({ name: 'read', arguments: { file_path: target } })
check('fence does not interfere with `read`', unrelatedTool === undefined, String(unrelatedTool))

// Editing the view must not touch the target.
const edited = opened.agentView.replace(
  'The compatibility layer gives an agent a familiar LaTeX view.',
  'The compatibility layer gives an agent a familiar LaTeX view.\n\n\\section{ Delivered by Harness}\n\nThis text was written in the LaTeX view.\n',
)
check('edit changes the view', edited !== opened.agentView)
const written = await call(tools, 'ros_agent_view', {
  action: 'write',
  session: 'harness-session',
  content: edited,
})
check('ros_agent_view write stores the view', written.stored === true && written.dirty === true)
check(
  'storing the view leaves the target file untouched',
  readFileSync(target, 'utf8') === originalTarget,
)

const readBack = await call(tools, 'ros_agent_view', { action: 'read', session: 'harness-session' })
check('ros_agent_view read returns the stored edit', readBack.agentView === edited)

const preview = await call(tools, 'ros_preview_delivery', { session: 'harness-session' })
check('preview returns target-format content', preview.targetContent.includes('Delivered by Harness'))
check('preview keeps the Typst-only directive', preview.targetContent.includes('#show heading'))
check('preview reports no drift', (preview.targetDrift || {}).drifted === false, JSON.stringify(preview.targetDrift))
check(
  'preview reports the round-trip verdict honestly',
  ['exact', 'unstable', 'unknown'].includes((preview.roundTrip || {}).status),
  JSON.stringify(preview.roundTrip).slice(0, 300),
)
const headingSpacingText = render(tools, 'ros_preview_delivery', {
  ...preview,
  roundTrip: {
    status: 'unstable',
    stable: false,
    formattingDifference: 'blank_line_after_latex_heading',
  },
})
check(
  'preview explains the known Typst heading-spacing difference without calling it exact',
  headingSpacingText.includes('only a blank line after a LaTeX heading differs')
    && headingSpacingText.includes('text check remains inconclusive')
    && !headingSpacingText.includes('round trip: exact'),
  headingSpacingText.slice(0, 500),
)
check(
  'the first delivery of a fresh artifact is not claimed stable',
  (preview.roundTrip || {}).status === 'exact'
    ? (preview.roundTrip || {}).stable === true
    : (preview.roundTrip || {}).stable === false,
  JSON.stringify(preview.roundTrip).slice(0, 200),
)
check(
  'an unstable verdict names which part differs',
  (preview.roundTrip || {}).status !== 'unstable'
    || (preview.roundTrip || {}).translatedByteIdentical === false
    || (preview.roundTrip || {}).preservedRegionsExact === false,
  JSON.stringify(preview.roundTrip).slice(0, 300),
)
check('preview did not write the target', readFileSync(target, 'utf8') === originalTarget)

// The confirmation gate must hold.
let gateHeld = false
try {
  await call(tools, 'ros_commit_delivery', { session: 'harness-session', userConfirmed: false })
} catch (error) {
  gateHeld = String(error.message).includes('USER_CONFIRMATION_REQUIRED')
}
check('commit refuses without user confirmation', gateHeld)
check('the refused commit wrote nothing', readFileSync(target, 'utf8') === originalTarget)

let agentCommitRefused = null
try {
  await call(tools, 'ros_commit_delivery', {
    session: 'harness-session',
    userConfirmed: true,
  })
} catch (error) {
  agentCommitRefused = error
}
check(
  'the agent tool cannot commit even with a model-supplied confirmation flag',
  agentCommitRefused
    && String(agentCommitRefused.message).includes('USER_CONFIRMATION_REQUIRED'),
  String(agentCommitRefused && agentCommitRefused.message),
)
check('the refused agent commit wrote nothing', readFileSync(target, 'utf8') === originalTarget)

// --- P0-2: the fence follows session state, not file existence -------------

// A session created for an artifact that does not exist yet must already be
// fenced, otherwise the agent can write the new path directly and then deliver
// over its own edit.
const createPath = join(workspace, 'created-not-yet.typ')
await call(tools, 'ros_create_artifact', {
  pair: 'LaTeX / Typst',
  targetPath: createPath,
  initialTargetContent: '= Created\n\nBody.\n',
  session: 'fence-create',
})
check('a created artifact does not exist on disk yet', !existsSync(createPath))
const createGuard = guards[0]({ name: 'write', arguments: { file_path: createPath } })
check(
  'fence denies `write` on a created-but-unwritten target',
  typeof createGuard === 'string' && createGuard.includes('ros_agent_view'),
  String(createGuard),
)

const familiarCreatePath = join(workspace, 'created-from-familiar-view.prql')
const familiarSql = 'SELECT * FROM sales'
const createdFromView = await call(tools, 'ros_create_artifact', {
  targetPath: familiarCreatePath,
  initialAgentView: familiarSql,
  session: 'create-from-view',
})
check(
  'new artifacts accept familiar-view content without asking the agent for target syntax',
  createdFromView.agentView === familiarSql
    && createdFromView.notes.some((note) => note.includes('Started from the familiar view'))
    && !existsSync(familiarCreatePath),
  JSON.stringify({ view: createdFromView.agentView, notes: createdFromView.notes }),
)
const familiarCreatePreview = await call(tools, 'ros_preview_delivery', {
  session: 'create-from-view',
})
check(
  'familiar-view creation translates during preview but still does not write the new target',
  familiarCreatePreview.dirty === true
    && typeof familiarCreatePreview.targetContent === 'string'
    && familiarCreatePreview.targetContent.length > 0
    && !existsSync(familiarCreatePath),
  JSON.stringify({ dirty: familiarCreatePreview.dirty, targetContent: familiarCreatePreview.targetContent }),
)
await call(tools, 'ros_close_session', { session: 'create-from-view' })

const markedCreatePath = join(workspace, 'created-with-target-block.typ')
await expectCode(
  'familiar-view creation refuses ungrounded pass-through blocks',
  () => call(tools, 'ros_create_artifact', {
    targetPath: markedCreatePath,
    initialAgentView: '// @TYPST-PASSTHROUGH\n#show: x\n// @END-TYPST-PASSTHROUGH\n',
  }),
  'INITIAL_VIEW_PASSTHROUGH',
)
check('refused pass-through create leaves the target absent', !existsSync(markedCreatePath))

const ambiguousCreatePath = join(workspace, 'created-with-ambiguous-input.typ')
await expectCode(
  'new artifact creation refuses mixed familiar and target input',
  () => call(tools, 'ros_create_artifact', {
    targetPath: ambiguousCreatePath,
    initialAgentView: '\\section{Familiar view}',
    initialTargetContent: '= Target view',
  }),
  'CREATE_CONTENT_AMBIGUOUS',
)
check('refused ambiguous create leaves the target absent', !existsSync(ambiguousCreatePath))

// Deleting the target underneath a session must not open a hole in the fence.
const vanishPath = join(workspace, 'vanishing.typ')
writeFileSync(vanishPath, '= Vanishing\n\nBody.\n', 'utf8')
await call(tools, 'ros_open_artifact', {
  pair: 'LaTeX / Typst', targetPath: vanishPath, session: 'fence-vanish',
})
rmSync(vanishPath, { force: true })
const vanishGuard = guards[0]({ name: 'write', arguments: { file_path: vanishPath } })
check(
  'fence still denies writes after the target is deleted externally',
  typeof vanishGuard === 'string',
  String(vanishGuard),
)
await call(tools, 'ros_close_session', { session: 'fence-create' })
await call(tools, 'ros_close_session', { session: 'fence-vanish' })
check(
  'closing those sessions releases the fence',
  guards[0]({ name: 'write', arguments: { file_path: vanishPath } }) === undefined,
)

// Path aliases must not walk around the fence: the key is the platform's
// canonical path, not the spelling the caller happened to use.
const aliasPath = join(workspace, 'alias-target.typ')
writeFileSync(aliasPath, '= Alias\n\nBody.\n', 'utf8')
await call(tools, 'ros_open_artifact', {
  pair: 'LaTeX / Typst', targetPath: aliasPath, session: 'fence-alias',
})
const aliasAttempts = {
  'trailing separator': `${aliasPath}\\`,
  'trailing dot': `${aliasPath}.`,
  'forward slashes': aliasPath.replace(/\\/g, '/'),
  'upper case': aliasPath.toUpperCase(),
  'redundant segments': aliasPath.replace('alias-target.typ', '.\\alias-target.typ'),
}
for (const [label, spelling] of Object.entries(aliasAttempts)) {
  const denial = guards[0]({ name: 'write', arguments: { file_path: spelling } })
  check(
    `fence denies the "${label}" spelling of a protected path`,
    typeof denial === 'string',
    `${spelling} -> ${String(denial)}`,
  )
}
// A relative path that resolves inside a configured root is covered too.
const relativeDenial = guards[0]({
  name: 'write',
  arguments: { file_path: aliasPath.slice(workspace.length + 1) },
})
check(
  'fence denies a relative path that names a protected target',
  typeof relativeDenial === 'string',
  String(relativeDenial),
)
await call(tools, 'ros_close_session', { session: 'fence-alias' })

// --- P0-3: the stability gate is enforced before the write ------------------
//
// A second, isolated harness with `requireStableRoundTrip: true`, so the flag is
// actually exercised instead of described. The check that matters is not the
// error message but the artifact: a refusal must leave the file byte-identical.
{
  const gateStateDir = join(workspace, '_harness_test_state_gated')
  rmSync(gateStateDir, { recursive: true, force: true })
  mkdirSync(gateStateDir, { recursive: true })
  const gated = makeContext()
  module.apply(gated.ctx, {
    ...config,
    stateFile: join(gateStateDir, 'sessions.sqlite3'),
    requireStableRoundTrip: true,
  })

  const gatedPath = join(workspace, 'gated-flat.typ')
  writeFileSync(gatedPath, '= Gated\n\nThe prototype is *ready for review*.\n', 'utf8')
  const opened = await call(gated.tools, 'ros_open_artifact', {
    pair: 'LaTeX / Typst', targetPath: gatedPath, session: 'gated-session',
  })
  const edited = `${opened.agentView}\n\\section{ Extra}\n\nMore text.\n`
  await call(gated.tools, 'ros_agent_view', {
    action: 'write', session: 'gated-session', content: edited,
  })

  const before = readFileSync(gatedPath, 'utf8')
  let gateRefused = false
  let gateMessage = ''
  // The plugin now enforces preview-before-write, so previewing is part of
  // reaching the stability gate rather than a step the test may skip.
  await call(gated.tools, 'ros_preview_delivery', { session: 'gated-session' }).catch(() => null)
  try {
    await call(gated.tools, 'ros_commit_delivery', {
      session: 'gated-session', userConfirmed: true,
    })
  } catch (error) {
    gateRefused = true
    gateMessage = String(error.message)
  }
  check(
    'an agent cannot bypass the stability configuration by calling commit',
    gateRefused && gateMessage.includes('USER_CONFIRMATION_REQUIRED'),
    gateMessage.slice(0, 300),
  )
  check(
    'a gated refusal left the target byte-identical',
    readFileSync(gatedPath, 'utf8') === before,
    'the target changed even though the delivery was refused',
  )
  const status = await call(gated.tools, 'ros_harness_status', { session: 'gated-session' })
  check(
    'a gated refusal kept the session and its pending edit',
    (status.sessions || []).some(
      (entry) => entry.session === 'gated-session' && entry.dirty === true,
    ),
    JSON.stringify(status.sessions || []).slice(0, 300),
  )
  check(
    'a gated refusal kept the write fence',
    typeof gated.guards[0]({ name: 'write', arguments: { file_path: gatedPath } }) === 'string',
  )

  await call(gated.tools, 'ros_close_session', { session: 'gated-session' })
  for (const leftover of ['gated-flat.typ', 'gated.typ']) {
    rmSync(join(workspace, leftover), { force: true })
  }
  rmSync(gateStateDir, { recursive: true, force: true })
}

// --- zero-config workspace derivation -------------------------------------
//
// With no `workspaceRoots`, the first open derives the root from the Host
// session's own cwd. It must not fall back to process.cwd() or accept an
// arbitrary path.
{
  const autoStateDir = join(workspace, '_harness_test_state_auto')
  rmSync(autoStateDir, { recursive: true, force: true })
  mkdirSync(autoStateDir, { recursive: true })
  const auto = makeContext({
    systemPrompt: {
      section(definition) {
        auto.promptSections.push(definition)
        return () => {}
      },
    },
    trustCheck(request) {
      return request?.headers?.['x-dsh-token'] === 'valid-token' ? undefined : 401
    },
  })
  module.apply(auto.ctx, {
    ...config,
    workspaceRoots: [],
    stateFile: join(autoStateDir, 'sessions.sqlite3'),
  })
  check(
    'zero-config chats receive Rosetta prompt guidance before workspace derivation',
    auto.promptSections.length === 1
      && auto.promptSections[0].text.includes('ros_open_artifact')
      && auto.promptSections[0].text.includes('initialAgentView')
      && auto.promptSections[0].text.includes('click Deliver')
      && auto.promptSections[0].text.includes('Prefer an absolute')
      && auto.promptSections[0].text.includes('workspace-relative path is also accepted')
      && auto.promptSections[0].text.includes('Do not search the workspace to')
      && auto.promptSections[0].text.includes('Never widen or bypass the workspace boundary')
      && auto.promptSections[0].text.includes('let the tool infer')
      && auto.promptSections[0].text.includes('Call `ros_list_pairs` only when the extension is ambiguous')
      && auto.promptSections[0].text.includes('Do not call `ros_doctor` before every routine edit')
      && auto.promptSections[0].text.includes('A `round trip: exact` result means only')
      && auto.promptSections[0].text.includes('It does not prove that the')
      && auto.promptSections[0].text.includes('requested change was made, that the target compiles')
      && auto.promptSections[0].text.includes('report compiler checks')
      && auto.promptSections[0].text.includes('separately when the tool result provides them')
      && auto.promptSections[0].text.includes('ros_harness_status')
      && auto.promptSections[0].text.includes('conversation history')
      && auto.promptSections[0].text.includes('outcome is unknown')
      && auto.promptSections[0].text.includes('ros_close_session` discards the session')
      && auto.promptSections[0].text.includes('only when the user explicitly asks to')
      && auto.promptSections[0].text.includes('interrupt the task with unrelated sessions')
      && !auto.promptSections[0].text.includes('Use `ros_close_session` when'),
    JSON.stringify(auto.promptSections).slice(0, 300),
  )
  const openPathDescription = auto.tools.get('ros_open_artifact')?.parameters?.properties?.targetPath?.description || ''
  const createPathDescription = auto.tools.get('ros_create_artifact')?.parameters?.properties?.targetPath?.description || ''
  check(
    'open/create path schemas agree with prompt guidance',
    [openPathDescription, createPathDescription].every((description) =>
      description.includes('Prefer an absolute path')
      && description.includes('workspace-relative path is also accepted')
      && description.includes('current workspace'),
    ),
    JSON.stringify({ openPathDescription, createPathDescription }),
  )
  check(
    'create tool documents familiar-view authoring as preferred',
    auto.tools.get('ros_create_artifact')?.parameters?.properties?.initialAgentView?.description?.includes('Preferred')
      && auto.tools.get('ros_create_artifact')?.description?.includes('Prefer `initialAgentView`'),
  )
  const beforeRoot = { status: null, body: '' }
  await auto.routes.get('/rosetta-harness/state').handler(
    { method: 'GET', url: '/rosetta-harness/state', headers: { 'x-dsh-token': 'valid-token' } },
    {
      writeHead(status) { beforeRoot.status = status },
      end(body) { beforeRoot.body = body || '' },
    },
  )
  check(
    'zero-config status explains setup without an HTTP service error',
    beforeRoot.status === 200
      && JSON.parse(beforeRoot.body).disabled === true,
    `status=${beforeRoot.status} body=${beforeRoot.body.slice(0, 180)}`,
  )
  try {
    await call(
      auto.tools,
      'ros_open_artifact',
      { targetPath: 'relative-no-root.typ', session: 'no-root' },
      { agent: { session: { id: DEFAULT_CHAT } } },
    )
    check('missing workspace root returns an actionable refusal', false, 'open unexpectedly succeeded')
  } catch (error) {
    check(
      'missing workspace root returns an actionable refusal',
      String(error.message).includes('NO_WORKSPACE_ROOT')
        && String(error.message).includes('setup.ps1'),
      String(error.message).slice(0, 300),
    )
  }
  const autoPath = join(workspace, 'auto-root.typ')
  writeFileSync(autoPath, '= Auto root\n\nBody.\n', 'utf8')
  const autoOpened = await call(
    auto.tools,
    'ros_open_artifact',
    { targetPath: autoPath, session: 'auto-root' },
    { agent: { session: { id: DEFAULT_CHAT, header: { cwd: workspace } } } },
  )
  check(
    'zero-config open derives the workspace root from the session cwd',
    autoOpened.pair === 'LaTeX / Typst' && autoOpened.session === 'auto-root',
    JSON.stringify(autoOpened).slice(0, 300),
  )
  const autoDoctor = await call(auto.tools, 'ros_doctor', {})
  check(
    'the derived root is published to diagnostics',
    autoDoctor.available === true
      && (autoDoctor.workspaceRoots || []).includes(workspace),
    JSON.stringify(autoDoctor.workspaceRoots || []).slice(0, 300),
  )
  await call(auto.tools, 'ros_close_session', { session: 'auto-root' })
  rmSync(autoPath, { force: true })
  rmSync(autoStateDir, { recursive: true, force: true })
}

// --- refusals -------------------------------------------------------------

async function expectCode(label, fn, code) {
  try {
    await fn()
    check(label, false, 'the call unexpectedly succeeded')
  } catch (error) {
    check(label, String(error.message).includes(code), String(error.message).slice(0, 300))
  }
}

async function expectActionHint(label, fn, code, requiredAction) {
  try {
    await fn()
    check(label, false, 'the call unexpectedly succeeded')
  } catch (error) {
    const message = String(error.message)
    check(
      label,
      message.includes(`${code}:`)
        && message.includes('What to do:')
        && message.includes(requiredAction),
      message.slice(0, 500),
    )
  }
}

const outsideDir = mkdtempSync(join(tmpdir(), 'rosetta-outside-'))
const outsideTarget = join(outsideDir, 'outside.typ')
writeFileSync(
  outsideTarget,
  '= Outside\n\nThis file exists and is readable, but it is not in any configured workspace root.\n',
  'utf8',
)

await expectCode(
  'refuses an existing absolute path outside the workspace',
  () => call(tools, 'ros_open_artifact', { pair: 'LaTeX / Typst', targetPath: outsideTarget }),
  'PATH_OUTSIDE_WORKSPACE',
)
await expectCode(
  'refuses an absolute path in another directory that does not exist',
  () => call(tools, 'ros_open_artifact', { pair: 'LaTeX / Typst', targetPath: join(outsideDir, 'nope.typ') }),
  'PATH_OUTSIDE_WORKSPACE',
)
await expectCode(
  'refuses a traversal out of the workspace',
  () =>
    call(tools, 'ros_open_artifact', {
      pair: 'LaTeX / Typst',
      targetPath: join(workspace, '..', '..', '..', 'definitely-outside.typ'),
    }),
  'PATH_OUTSIDE_WORKSPACE',
)
await expectActionHint(
  'missing target explains open-versus-create recovery',
  () => call(tools, 'ros_open_artifact', { pair: 'LaTeX / Typst', targetPath: join(workspace, 'absent-xyz.typ') }),
  'TARGET_NOT_FOUND',
  'for a new file, use ros_create_artifact',
)
await expectActionHint(
  'missing parent explains that the user must create the directory',
  () => call(tools, 'ros_create_artifact', {
    targetPath: join(workspace, 'parent-does-not-exist', 'new.typ'),
    initialTargetContent: '= New\n',
  }),
  'PARENT_MISSING',
  'create the parent directory first',
)
await expectActionHint(
  'extension mismatch points to a compatible pair',
  () => call(tools, 'ros_open_artifact', { pair: 'PRQL / SQL', targetPath: target }),
  'EXTENSION_MISMATCH',
  'ros_list_pairs',
)
await expectActionHint(
  'create-over-existing explains open-or-new-path recovery',
  () => call(tools, 'ros_create_artifact', {
    targetPath: target,
    initialTargetContent: '= Replacement\n',
  }),
  'TARGET_EXISTS',
  'ros_open_artifact to edit that file',
)
await expectActionHint(
  'an unfamiliar extension explains how to select a pair',
  () => call(tools, 'ros_open_artifact', { targetPath: join(workspace, 'unknown-format.xyz') }),
  'UNKNOWN_PAIR',
  'call ros_list_pairs',
)
const notUtf8Target = join(workspace, 'not-utf8.typ')
writeFileSync(notUtf8Target, Buffer.from([0xff, 0xfe]))
await expectActionHint(
  'non-UTF-8 target explains the supported encoding',
  () => call(tools, 'ros_open_artifact', { targetPath: notUtf8Target }),
  'NOT_UTF8',
  'save or convert the target file as UTF-8',
)
await expectCode(
  'refuses an unknown pair',
  () => call(tools, 'ros_open_artifact', { pair: 'LaTeX / TypeScript', targetPath: target }),
  'UNKNOWN_PAIR',
)
await expectActionHint(
  'missing familiar-view content names the required field',
  () => call(tools, 'ros_agent_view', { action: 'write', session: 'any-session' }),
  'CONTENT_REQUIRED',
  'complete familiar view in the content field',
)
await expectActionHint(
  'oversized create advises reducing content to the configured limit',
  () => call(tools, 'ros_create_artifact', {
    targetPath: join(workspace, 'oversized.typ'),
    initialTargetContent: 'x'.repeat(2_000_001),
  }),
  'ARTIFACT_TOO_LARGE',
  '2,000,000 UTF-8 bytes',
)
await expectCode(
  'refuses an unknown session',
  () => call(tools, 'ros_agent_view', { action: 'read', session: 'no-such-session' }),
  'UNKNOWN_SESSION',
)
await expectCode(
  'refuses the plugin state file as an artifact',
  () =>
    call(tools, 'ros_open_artifact', {
      pair: 'LaTeX / Typst',
      targetPath: join(workspace, '.rosetta', 'harness-sessions.json'),
    }),
  'PATH_IS_PLUGIN_STATE',
)

// External modification must be detected before an overwrite.
const staleTarget = join(workspace, 'stale-harness.typ')
writeFileSync(staleTarget, '= Stale\n\nOriginal text.\n', 'utf8')
await call(tools, 'ros_open_artifact', {
  pair: 'LaTeX / Typst',
  targetPath: staleTarget,
  session: 'stale-session',
})
const staleView = await call(tools, 'ros_agent_view', { action: 'read', session: 'stale-session' })
await call(tools, 'ros_agent_view', {
  action: 'write',
  session: 'stale-session',
  content: staleView.agentView + '\n\\section{ Extra}\n\nAdded.\n',
})
writeFileSync(staleTarget, '#set page(width: 5cm)\n\n= Stale\n\nChanged outside Rosetta.\n', 'utf8')
// Preview first: the plugin refuses a commit that was never previewed, and the
// drift refusal is what this check is about.
await call(tools, 'ros_preview_delivery', { session: 'stale-session' }).catch(() => null)
await expectCode(
  'an agent commit cannot bypass an externally changed target',
  () => call(tools, 'ros_commit_delivery', { session: 'stale-session', userConfirmed: true }),
  'USER_CONFIRMATION_REQUIRED',
)
check(
  'the externally changed target was not overwritten',
  readFileSync(staleTarget, 'utf8').includes('Changed outside Rosetta'),
)

// --- status surface -------------------------------------------------------

const status = await call(tools, 'ros_harness_status', {})
check('ros_harness_status lists live sessions', status.count >= 2, JSON.stringify(status).slice(0, 300))
check(
  'status marks the externally changed target as drifted',
  (status.sessions || []).some((entry) => (entry.targetDrift || {}).drifted === true),
  JSON.stringify(status.sessions || []).slice(0, 400),
)

// The status route must answer the Client half - and only an authenticated one.
const route = routes.get('/rosetta-harness/state')

function makeResponse() {
  const captured = { status: null, body: '', headers: null }
  return {
    captured,
    response: {
      writeHead(status, headers) {
        captured.status = status
        captured.headers = headers || null
      },
      end(body) {
        captured.body = body || ''
      },
    },
  }
}

async function fetchRoute({ headers = {}, url = '/rosetta-harness/state', method = 'GET' } = {}) {
  const holder = makeResponse()
  await route.handler({ method, url, headers }, holder.response)
  return holder.captured
}

// P1-2: an unauthenticated caller must get a refusal with no body.
// The refusal still carries the build-identity headers: a local operator script
// cannot authenticate, and a 401 alone must not be read as "new build is live".
{
  const anonymous = await fetchRoute()
  check(
    'status route refuses an unauthenticated caller',
    anonymous.status === 401,
    `status=${anonymous.status} body=${String(anonymous.body).slice(0, 120)}`,
  )
  check(
    'the refusal carries no artifact text',
    !anonymous.body || anonymous.body.length === 0,
    `body=${String(anonymous.body).slice(0, 160)}`,
  )
  check(
    'the refusal still carries the running generation digest',
    anonymous.headers?.['x-rosetta-generation'] === expectedGeneration,
    `header=${anonymous.headers?.['x-rosetta-generation']} expected=${expectedGeneration}`,
  )
  check(
    'the refusal still advertises the running tool surface',
    String(anonymous.headers?.['x-rosetta-tools'] || '').split(',').includes('ros_maintenance'),
    `tools=${anonymous.headers?.['x-rosetta-tools']}`,
  )
  const forged = await fetchRoute({ headers: { origin: 'http://evil.example', 'x-dsh-token': 'nope' } })
  check('status route refuses a forged token', forged.status === 401, `status=${forged.status}`)
}

const authorized = await fetchRoute({ headers: { 'x-dsh-token': 'valid-token' } })
let snapshot = null
try {
  snapshot = JSON.parse(authorized.body)
} catch (error) {
  check('status route returns JSON for an authenticated caller', false, String(error))
}
if (snapshot) {
  check('status route reports ok', snapshot.ok === true, JSON.stringify(snapshot).slice(0, 300))
  check(
    'the authenticated payload carries the same generation as the header',
    snapshot.generation?.digest === expectedGeneration,
    `body=${snapshot.generation?.digest} expected=${expectedGeneration}`,
  )
  check(
    'status metadata carries no artifact bodies by default',
    snapshot.contentIncluded === false
      && (snapshot.sessions || []).every(
        (entry) => !entry.agentViewPreview && !entry.preview,
      ),
    JSON.stringify(snapshot.sessions || []).slice(0, 400),
  )
  check(
    'status metadata still identifies the open artifact',
    (snapshot.sessions || []).every((entry) => typeof entry.targetPath === 'string' && entry.pair),
  )
  check(
    'status without a chat id is explicitly workspace-scoped',
    snapshot.chatScoped === false && snapshot.otherSessionCount === 0,
    JSON.stringify({ chatScoped: snapshot.chatScoped, otherSessionCount: snapshot.otherSessionCount }),
  )
  check(
    'status carries per-pair availability',
    Array.isArray(snapshot.pairs)
      && snapshot.pairs.length === 4
      && snapshot.pairs.every(
        (pair) => typeof pair.name === 'string'
          && typeof pair.available === 'boolean'
          && Array.isArray(pair.missingRequired),
      ),
    JSON.stringify(snapshot.pairs || []).slice(0, 500),
  )
}

const withContent = await fetchRoute({
  headers: { 'x-dsh-token': 'valid-token' },
  url: `/rosetta-harness/state?content=1&chat=${encodeURIComponent(DEFAULT_CHAT)}`,
})
const contentSnapshot = JSON.parse(withContent.body)
check(
  'an explicit content request carries the agent view',
  contentSnapshot.contentIncluded === true
    && (contentSnapshot.sessions || []).some(
      (entry) => typeof entry.agentViewPreview === 'string' && entry.agentViewPreview.length > 0,
    ),
  JSON.stringify(contentSnapshot.sessions || []).slice(0, 300),
)

// A chat-scoped request must not inherit another chat's sessions. All sessions
// opened by `call` above belong to DEFAULT_CHAT.
{
  const scopedA = await fetchRoute({
    headers: { 'x-dsh-token': 'valid-token' },
    url: `/rosetta-harness/state?chat=${encodeURIComponent(DEFAULT_CHAT)}`,
  })
  const scopedABody = JSON.parse(scopedA.body)
  check(
    'status route scopes sessions to the named chat',
    scopedABody.chatScoped === true
      && (scopedABody.sessions || []).length > 0
      && (scopedABody.sessions || []).every((entry) => entry.ownerSessionId === DEFAULT_CHAT),
    JSON.stringify(scopedABody.sessions || []).slice(0, 400),
  )

  const scopedB = await fetchRoute({
    headers: { 'x-dsh-token': 'valid-token' },
    url: '/rosetta-harness/state?chat=chat-B',
  })
  const scopedBBody = JSON.parse(scopedB.body)
  check(
    'a fresh chat receives no session from another chat',
    scopedBBody.chatScoped === true
      && (scopedBBody.sessions || []).length === 0
      && scopedBBody.otherSessionCount > 0,
    JSON.stringify(scopedBBody).slice(0, 400),
  )
}

// The browser controls must share the bridge's preview gate and path fence.
const actionRoute = routes.get('/rosetta-harness/action')
async function postAction(
  action,
  session,
  headers = { 'x-dsh-token': 'valid-token', 'content-type': 'application/json' },
  harnessSessionId = DEFAULT_CHAT,
  extra = {},
) {
  const holder = makeResponse()
  const request = Readable.from([
    JSON.stringify({
      action,
      ...(session ? { session } : {}),
      ...extra,
      ...(harnessSessionId === null ? {} : { harnessSessionId }),
    }),
  ])
  request.method = 'POST'
  request.headers = headers
  await actionRoute.handler(request, holder.response)
  return holder.captured
}

async function chatEntry(sessionId, chat = DEFAULT_CHAT) {
  const response = await fetchRoute({
    headers: { 'x-dsh-token': 'valid-token' },
    url: `/rosetta-harness/state?chat=${encodeURIComponent(chat)}`,
  })
  const body = JSON.parse(response.body)
  return (body.sessions || []).find((entry) => entry.session === sessionId) || null
}

function confirmationFor(entry) {
  return {
    expectedPreviewSha256: entry.previewSha256,
    expectedViewSha256: entry.viewSha256,
    expectedTargetSha256: entry.targetSha256,
  }
}
const actionTarget = join(workspace, 'browser-action.typ')
writeFileSync(actionTarget, '= Browser action\n\nBefore.\n', 'utf8')
const actionOpen = await call(tools, 'ros_open_artifact', {
  pair: 'LaTeX / Typst', targetPath: actionTarget, session: 'browser-action',
})
await call(tools, 'ros_agent_view', {
  action: 'write', session: 'browser-action',
  content: actionOpen.agentView.replace('Before.', 'After.'),
})
const originalActionTarget = readFileSync(actionTarget, 'utf8')
const anonymousAction = await postAction('close', 'browser-action', { 'content-type': 'application/json' })
check('action route refuses anonymous callers without a response body', anonymousAction.status === 401 && !anonymousAction.body)
const premature = await postAction('commit', 'browser-action')
check('browser delivery requires a preview', premature.status === 409 && premature.body.includes('PREVIEW_REQUIRED'))
check('rejected browser delivery leaves the file unchanged', readFileSync(actionTarget, 'utf8') === originalActionTarget)
const browserPreview = await postAction('preview', 'browser-action')
check('browser preview succeeds without writing', browserPreview.status === 200 && readFileSync(actionTarget, 'utf8') === originalActionTarget)
const previewEntry = await chatEntry('browser-action')
check(
  'the state carries the preview, view and target digests',
  previewEntry
    && typeof previewEntry.previewSha256 === 'string'
    && typeof previewEntry.viewSha256 === 'string'
    && typeof previewEntry.targetSha256 === 'string',
  JSON.stringify(previewEntry || {}).slice(0, 500),
)
const staleCommit = await postAction('commit', 'browser-action', undefined, DEFAULT_CHAT, {
  ...confirmationFor(previewEntry),
  expectedPreviewSha256: '0'.repeat(64),
})
check(
  'a confirmation bound to a different preview is refused',
  staleCommit.status === 409 && staleCommit.body.includes('CONFIRMATION_STALE'),
  `status=${staleCommit.status} body=${String(staleCommit.body).slice(0, 300)}`,
)
check('a stale confirmation wrote nothing', readFileSync(actionTarget, 'utf8') === originalActionTarget)
const browserDiscard = await postAction('discard-preview', 'browser-action')
check(
  'browser discard drops the candidate without writing',
  browserDiscard.status === 200
    && browserDiscard.body.includes('"discarded":true')
    && readFileSync(actionTarget, 'utf8') === originalActionTarget,
)
const browserPreviewAgain = await postAction('preview', 'browser-action')
check('a discarded candidate can be previewed again', browserPreviewAgain.status === 200)
const previewAgainEntry = await chatEntry('browser-action')
const browserCommit = await postAction('commit', 'browser-action', undefined, DEFAULT_CHAT, {
  ...confirmationFor(previewAgainEntry),
})
check('browser delivery writes the preview', browserCommit.status === 200 && readFileSync(actionTarget, 'utf8').includes('After.'))
const committedEntry = await chatEntry('browser-action')
check('a delivered session reports an available restore', committedEntry?.restoreAvailable === true)
const browserRestore = await postAction('restore-last', 'browser-action')
check(
  'the previous delivered version can be restored',
  browserRestore.status === 200
    && readFileSync(actionTarget, 'utf8') === originalActionTarget,
  `status=${browserRestore.status} body=${String(browserRestore.body).slice(0, 300)}`,
)
check(
  'restoring the previous version closes the session and frees the fence',
  guard({ name: 'write', arguments: { file_path: actionTarget } }) === undefined,
)

// A confirmation must fail if the agent edits the view after the user saw the
// preview; the old target and the old view digest must both matter.
{
  const confirmTarget = join(workspace, 'confirm-view.typ')
  writeFileSync(confirmTarget, '= Confirm view\n\nBefore.\n', 'utf8')
  const opened = await call(tools, 'ros_open_artifact', {
    pair: 'LaTeX / Typst',
    targetPath: confirmTarget,
    session: 'confirm-view',
  })
  await call(tools, 'ros_agent_view', {
    action: 'write',
    session: 'confirm-view',
    content: opened.agentView.replace('Before.', 'First edit.'),
  })
  await postAction('preview', 'confirm-view')
  const firstEntry = await chatEntry('confirm-view')
  await call(tools, 'ros_agent_view', {
    action: 'write',
    session: 'confirm-view',
    content: opened.agentView.replace('Before.', 'Second edit.'),
  })
  const staleViewCommit = await postAction('commit', 'confirm-view', undefined, DEFAULT_CHAT, {
    ...confirmationFor(firstEntry),
  })
  check(
    'a confirmation becomes stale when the agent edits the view',
    staleViewCommit.status === 409 && staleViewCommit.body.includes('CONFIRMATION_STALE'),
    `status=${staleViewCommit.status} body=${String(staleViewCommit.body).slice(0, 300)}`,
  )
  check(
    'the stale view confirmation wrote nothing',
    readFileSync(confirmTarget, 'utf8') === '= Confirm view\n\nBefore.\n',
  )
  await postAction('preview', 'confirm-view')
  const secondEntry = await chatEntry('confirm-view')
  const freshCommit = await postAction('commit', 'confirm-view', undefined, DEFAULT_CHAT, {
    ...confirmationFor(secondEntry),
  })
  check(
    'a fresh preview and confirmation writes the new candidate',
    freshCommit.status === 200 && readFileSync(confirmTarget, 'utf8').includes('Second edit.'),
    `status=${freshCommit.status} body=${String(freshCommit.body).slice(0, 300)}`,
  )
  await call(tools, 'ros_close_session', { session: 'confirm-view' })
  rmSync(confirmTarget, { force: true })
}

// Explicit cleanup from the other-session panel is allowed only with the
// confirmation flag; it removes the session but not any file.
{
  const releaseTarget = join(workspace, 'release-other.typ')
  writeFileSync(releaseTarget, '= Release me\n\nBody.\n', 'utf8')
  await call(
    tools,
    'ros_open_artifact',
    { pair: 'LaTeX / Typst', targetPath: releaseTarget, session: 'release-other' },
    { agent: { session: { id: 'chat-C' } } },
  )
  const refusedRelease = await postAction('release-other', 'release-other', undefined, DEFAULT_CHAT)
  check(
    'releasing another chat session requires confirmDiscard',
    refusedRelease.status === 400 && refusedRelease.body.includes('confirmDiscard'),
    `status=${refusedRelease.status} body=${String(refusedRelease.body).slice(0, 300)}`,
  )
  const released = await postAction('release-other', 'release-other', undefined, DEFAULT_CHAT, {
    confirmDiscard: true,
  })
  check(
    'the other-session panel can explicitly release a session',
    released.status === 200 && released.body.includes('"action":"release-other"'),
    `status=${released.status} body=${String(released.body).slice(0, 300)}`,
  )
  const chatCState = await fetchRoute({
    headers: { 'x-dsh-token': 'valid-token' },
    url: '/rosetta-harness/state?chat=chat-C',
  })
  check(
    'the released session is gone from its own chat',
    !(JSON.parse(chatCState.body).sessions || []).some(
      (entry) => entry.session === 'release-other',
    ),
    String(chatCState.body).slice(0, 300),
  )
  rmSync(releaseTarget, { force: true })
}

// --- chat ownership -------------------------------------------------------
//
// A status strip for chat B must not be able to preview or deliver chat A's
// session, and an unattributed session must not be claimable by any chat.
const chatBTarget = join(workspace, 'chat-b.typ')
writeFileSync(chatBTarget, '= Chat B\n\nBody.\n', 'utf8')
await call(
  tools,
  'ros_open_artifact',
  { pair: 'LaTeX / Typst', targetPath: chatBTarget, session: 'chat-b' },
  { agent: { session: { id: 'chat-B' } } },
)
async function expectOwnershipRefusal(label, fn) {
  try {
    await fn()
    check(label, false, 'the call unexpectedly succeeded')
  } catch (error) {
    check(label, String(error.message).includes('SESSION_OWNERSHIP_MISMATCH'), String(error.message))
  }
}
await expectOwnershipRefusal(
  'agent read refuses another chat\'s session',
  () => call(tools, 'ros_agent_view', { action: 'read', session: 'chat-b' }),
)
await expectOwnershipRefusal(
  'agent write refuses another chat\'s session',
  () => call(tools, 'ros_agent_view', {
    action: 'write',
    session: 'chat-b',
    content: '= hijacked\n',
  }),
)
await expectOwnershipRefusal(
  'agent preview refuses another chat\'s session',
  () => call(tools, 'ros_preview_delivery', { session: 'chat-b' }),
)
const currentChatStatus = await call(tools, 'ros_harness_status', {})
check(
  'agent status only lists the current chat\'s sessions',
  !(currentChatStatus.sessions || []).some((entry) => entry.session === 'chat-b'),
  JSON.stringify(currentChatStatus.sessions || []).slice(0, 400),
)
const foreignPreview = await postAction('preview', 'chat-b', undefined, DEFAULT_CHAT)
check(
  'browser preview refuses a session owned by another chat',
  foreignPreview.status === 409 && foreignPreview.body.includes('SESSION_OWNERSHIP_MISMATCH'),
  `status=${foreignPreview.status} body=${String(foreignPreview.body).slice(0, 300)}`,
)
const ownPreview = await postAction('preview', 'chat-b', undefined, 'chat-B')
check(
  'browser preview accepts the owning chat',
  ownPreview.status === 200,
  `status=${ownPreview.status} body=${String(ownPreview.body).slice(0, 300)}`,
)
const noChatPreview = await postAction('preview', 'chat-b', undefined, null)
check(
  'browser preview refuses when the strip cannot name its chat',
  noChatPreview.status === 400 && noChatPreview.body.includes('CHAT_SESSION_REQUIRED'),
  `status=${noChatPreview.status} body=${String(noChatPreview.body).slice(0, 300)}`,
)

// The "+N other" panel can open another chat's target as a new session in
// this chat. The old session is untouched; the new one is owned here.
const adopted = await postAction('open', null, undefined, DEFAULT_CHAT, {
  targetPath: chatBTarget,
  pair: 'LaTeX / Typst',
})
let adoptedSession = null
let adoptedPayload = null
try {
  adoptedPayload = JSON.parse(adopted.body)
  adoptedSession = adoptedPayload.session
} catch {
  adoptedSession = null
}
check(
  'browser open-in-this-chat creates a new session',
  adopted.status === 200
    && Boolean(adoptedSession)
    && adoptedPayload?.action === 'open'
    && adoptedPayload?.targetPath === chatBTarget,
  `status=${adopted.status} body=${String(adopted.body).slice(0, 300)}`,
)
if (adoptedSession) {
  const adoptedState = await fetchRoute({
    headers: { 'x-dsh-token': 'valid-token' },
    url: `/rosetta-harness/state?chat=${encodeURIComponent(DEFAULT_CHAT)}`,
  })
  const adoptedBody = JSON.parse(adoptedState.body)
  check(
    'the adopted session belongs to the current chat',
    (adoptedBody.sessions || []).some(
      (entry) => entry.session === adoptedSession && entry.ownerSessionId === DEFAULT_CHAT,
    ),
    JSON.stringify(adoptedBody.sessions || []).slice(0, 400),
  )
  await call(tools, 'ros_close_session', { session: adoptedSession })
}

const unownedTarget = join(workspace, 'unowned.typ')
writeFileSync(unownedTarget, '= Unowned\n\nBody.\n', 'utf8')
let unownedOpenError = null
try {
  await call(
    tools,
    'ros_open_artifact',
    { pair: 'LaTeX / Typst', targetPath: unownedTarget, session: 'unowned' },
    { agent: undefined },
  )
} catch (error) {
  unownedOpenError = error
}
check(
  'an agent open without a chat identity is refused before creating a session',
  unownedOpenError && String(unownedOpenError.message).includes('CHAT_SESSION_REQUIRED'),
  String(unownedOpenError && unownedOpenError.message),
)
await call(
  tools,
  'ros_close_session',
  { session: 'chat-b' },
  { agent: { session: { id: 'chat-B' } } },
)

const closed = await call(tools, 'ros_close_session', { session: 'stale-session' })
check('ros_close_session reports the discarded edits', closed.closed === true && closed.discardedPendingEdits === true)

// --- summary --------------------------------------------------------------

for (const leftover of [
  'harness-loop.typ',
  'alias-target.typ',
  'stale-harness.typ',
  'browser-action.typ',
  'chat-b.typ',
  'unowned.typ',
]) {
  rmSync(join(workspace, leftover), { force: true })
}
rmSync(stateDir, { recursive: true, force: true })
rmSync(outsideDir, { recursive: true, force: true })
const relativeWorkspace = relative(workspaceBase, workspace)
if (
  !relativeWorkspace
  || isAbsolute(relativeWorkspace)
  || relativeWorkspace.startsWith('..')
  || !basename(workspace).startsWith('h-')
) {
  throw new Error(`Refusing to remove a test directory outside its isolated workspace: ${workspace}`)
}
rmSync(workspace, { recursive: true, force: true })

const failures = results.filter((entry) => !entry.ok)
console.log('\n' + '='.repeat(72))
console.log(`${results.length - failures.length}/${results.length} checks passed`)
for (const failure of failures) console.log(`  FAILED: ${failure.label}\n          ${failure.detail}`)
process.exit(failures.length ? 1 : 0)

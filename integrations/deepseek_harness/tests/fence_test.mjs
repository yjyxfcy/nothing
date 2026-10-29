/**
 * Fence-integrity tests for the Rosetta Harness plugin.
 *
 * The path fence is the plugin's main safety property, and its two failure modes
 * are about *timing*, not translation - which is why they need their own file
 * with a scriptable bridge:
 *
 *   1. **Unknown is not empty.** The session list is read asynchronously. If that
 *      read fails, or simply has not finished, the fence used to be empty, which
 *      reads exactly like "nothing is managed" and let a native `write`/`edit`
 *      through a path Rosetta owned. The guard must refuse conservatively inside
 *      the configured roots while the state is unknown.
 *
 *   2. **A stale answer must not win.** Several refreshes overlap. An earlier
 *      query returning later carries a list from before a session was opened, so
 *      applying it would *drop* the fence on a path that is now managed.
 *
 * `tests/stub_bridge.py` supplies the timing and the payloads; the plugin is
 * loaded exactly as the Harness loads it.
 *
 * Run:  node tests/fence_test.mjs [writable-base-dir]
 *
 * The base directory matters for the corrupt-store case: that scenario runs the
 * REAL bridge, which needs to create `<root>/.rosetta/tmp`, and the sandbox does
 * not permit that under the system temp directory. It defaults to the plugin's
 * own `demo_workspace`, which is inside the DSH workspace and therefore writable.
 */
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const PLUGIN = resolve(HERE, '..', 'index.js')
const STUB = resolve(HERE, 'stub_bridge.py')

const PYTHON = process.env.ROSETTA_TEST_PYTHON || ''
if (!PYTHON) {
  console.error('ROSETTA_TEST_PYTHON must point at the interpreter used for the tests')
  process.exit(2)
}
const TEST_EXEC = { agent: { session: { id: 'fence-test-chat' } } }

const results = []
function check(label, condition, detail = '') {
  results.push({ label, ok: Boolean(condition), detail })
  console.log(`[${condition ? 'PASS' : 'FAIL'}] ${label}${condition || !detail ? '' : ` :: ${detail}`}`)
}

const baseDir = process.argv[2] ? resolve(process.argv[2]) : resolve(HERE, '..', 'demo_workspace')
mkdirSync(baseDir, { recursive: true })
const root = mkdtempSync(join(baseDir, 'fence-'))
const workspace = join(root, 'workspace')
const outside = join(root, 'outside')
mkdirSync(workspace, { recursive: true })
mkdirSync(outside, { recursive: true })

const module = await import(`file://${PLUGIN.replace(/\\/g, '/')}`)

function makeContext() {
  const tools = new Map()
  const guards = []
  const routes = new Map()
  const ctx = {
    tools: {
      register(definition) {
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
        routes.set(route.path, route)
        return () => routes.delete(route.path)
      },
    },
    get() {
      return undefined
    },
    effect(callback) {
      return callback()
    },
  }
  return { ctx, tools, guards, routes }
}

/**
 * Start the plugin with a scripted stub bridge.
 *
 * `spec.status` is a queue consumed one entry per `status` call, so a test can
 * decide what the first query answers, what the second answers, and how long each
 * takes.
 */
function startPlugin({ spec, workspaceRoots = [workspace] }) {
  const specPath = join(root, `spec-${Math.random().toString(36).slice(2)}.json`)
  const counterPath = `${specPath}.count`
  const markerPath = `${specPath}.seen`
  writeFileSync(specPath, JSON.stringify(spec), 'utf8')
  writeFileSync(markerPath, '', 'utf8')
  process.env.ROSETTA_STUB_SPEC = specPath
  process.env.ROSETTA_STUB_COUNTER = counterPath
  process.env.ROSETTA_STUB_MARKER = markerPath
  const made = makeContext()
  module.apply(made.ctx, {
    pythonExecutable: PYTHON,
    workspaceRoots,
    bridgePath: STUB,
    stateFile: join(root, 'unused.sqlite3'),
  })
  made.specPath = specPath
  made.counterPath = counterPath
  made.markerPath = markerPath
  return made
}

function countLines(path) {
  try {
    return readFileSync(path, 'utf8').split('\n').filter((line) => line.trim()).length
  } catch {
    return 0
  }
}

/** Block until a stub call has been observed, so tests can sequence themselves. */
async function waitForLines(path, wanted, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (countLines(path) >= wanted) return true
    await new Promise((done) => setTimeout(done, 50))
  }
  return false
}

const session = (id, targetPath, pair = 'LaTeX / Typst') => ({ session: id, pair, targetPath, dirty: false })
const guardDenies = (made, name, target) =>
  typeof made.guards[0]({ name, arguments: { file_path: target } }) === 'string'
const guardMessage = (made, name, target) => made.guards[0]({ name, arguments: { file_path: target } })

// ---------------------------------------------------------------------------
// 1. Unknown state must refuse inside the configured roots
// ---------------------------------------------------------------------------

{
  // The first (and only) status answer never arrives in time.
  const made = startPlugin({
    spec: { status: [{ delayMs: 4000, sessions: [] }] },
  })
  const insidePath = join(workspace, 'possibly-managed.typ')
  const outsidePath = join(outside, 'unrelated.typ')

  // Checked immediately, while the seeding refresh is still in flight. `apply`
  // has returned, so the guard exists; nothing has been learned yet.
  check(
    'a write inside a configured root is refused while the fence is not ready',
    guardDenies(made, 'write', insidePath),
    String(guardMessage(made, 'write', insidePath)).slice(0, 200),
  )
  check(
    'the refusal says the state is unknown rather than claiming the path is managed',
    /cannot currently tell whether/.test(String(guardMessage(made, 'write', insidePath))),
    String(guardMessage(made, 'write', insidePath)).slice(0, 200),
  )
  check(
    'an edit inside a configured root is refused too',
    guardDenies(made, 'edit', insidePath),
  )
  check(
    'a write OUTSIDE every configured root is not refused',
    !guardDenies(made, 'write', outsidePath),
    String(guardMessage(made, 'write', outsidePath)),
  )
  check(
    'a non-mutating tool is never refused',
    !guardDenies(made, 'read', insidePath),
  )

  // Let the slow answer land, then the fence must be ready and the untouched
  // path must be writable - the conservative refusal is temporary, not permanent.
  await new Promise((done) => setTimeout(done, 5200))
  check(
    'once the state is known, an unmanaged path inside the root is allowed again',
    !guardDenies(made, 'write', insidePath),
    String(guardMessage(made, 'write', insidePath)),
  )
}

// ---------------------------------------------------------------------------
// 2. A failed bridge must keep refusing, not silently allow
// ---------------------------------------------------------------------------

for (const [label, spec] of [
  ['the bridge exits non-zero', { status: [{ sessions: [] }], exitCode: 3 }],
  ['the bridge returns unparseable output', { status: [{ sessions: [] }], garbage: true }],
  ['the bridge reports a corrupt store', { status: [{ sessions: [] }], okFalse: true }],
  ['the bridge answers without ok', { status: [{ sessions: [], omitOk: true }] }],
]) {
  const made = startPlugin({ spec })
  const insidePath = join(workspace, 'still-unknown.typ')
  await new Promise((done) => setTimeout(done, 800))
  check(
    `a failed status read still refuses writes in the root (${label})`,
    guardDenies(made, 'write', insidePath),
    String(guardMessage(made, 'write', insidePath)).slice(0, 200),
  )
  check(
    `a failed status read does not refuse outside the roots (${label})`,
    !guardDenies(made, 'write', join(outside, 'unrelated.typ')),
  )
}

// ---------------------------------------------------------------------------
// 3. A stale status answer must not overwrite a newer fence
// ---------------------------------------------------------------------------

{
  const managed = join(workspace, 'managed.typ')
  const openedLater = join(workspace, 'opened-later.typ')
  const made = startPlugin({
    spec: { status: [{ delayMs: 0, sessions: [] }], default: { ok: true } },
  })

  // Let activation finish so the fence is ready and empty.
  await waitForLines(made.markerPath, 1)
  await new Promise((done) => setTimeout(done, 250))

  const answerPath = `${made.specPath}.answer`
  // The OLD truth: only `managed` is fenced. Slow, so it returns last.
  writeFileSync(answerPath, JSON.stringify({ delayMs: 2000, sessions: [session('old', managed)] }), 'utf8')

  const before = countLines(made.markerPath)
  const first = made.tools
    .get('ros_close_session')
    .execute({ session: 'anything' }, TEST_EXEC)
    .catch(() => null)
  // Wait until the first refresh has *read* the old answer, so the rewrite below
  // cannot be observed by it. Without this handshake the two short-lived
  // processes could pick up the script out of order and the test would prove
  // nothing.
  await waitForLines(made.markerPath, before + 1)

  // The NEW truth, including a session opened after the first query was issued.
  // Fast, so the newer ticket applies first.
  writeFileSync(answerPath, JSON.stringify({ delayMs: 0, sessions: [session('old', managed), session('new', openedLater)] }), 'utf8')

  const second = made.tools
    .get('ros_close_session')
    .execute({ session: 'anything' }, TEST_EXEC)
    .catch(() => null)
  await Promise.all([first, second])
  // Wait past the slow, stale answer so it has had every chance to do damage.
  await new Promise((done) => setTimeout(done, 2600))

  check(
    'the newer fence kept the session that the stale answer did not know about',
    guardDenies(made, 'write', openedLater),
    String(guardMessage(made, 'write', openedLater)).slice(0, 220),
  )
  check(
    'the path both answers agreed on is still fenced',
    guardDenies(made, 'write', managed),
    String(guardMessage(made, 'write', managed)).slice(0, 220),
  )
  check(
    'a path in neither answer is still allowed',
    !guardDenies(made, 'write', join(workspace, 'no-such-session.typ')),
  )
}

// ---------------------------------------------------------------------------
// 4. A later failure must not release an already-good fence
// ---------------------------------------------------------------------------

{
  const managed = join(workspace, 'kept.typ')
  const made = startPlugin({
    spec: {
      status: [
        { delayMs: 0, sessions: [session('kept', managed)] },
        // The store then breaks. Clearing the fence here would *release* the
        // protection on a path that may still be managed.
        { sessions: [], okFalse: true },
      ],
      default: { ok: true },
    },
  })
  // Trigger a refresh that will fail, now that a good list is already applied.
  await made.tools
    .get('ros_close_session')
    .execute({ session: 'anything' }, TEST_EXEC)
    .catch(() => null)
  await new Promise((done) => setTimeout(done, 600))

  check(
    'a refresh failure after a good load does not drop the fence',
    guardDenies(made, 'write', managed),
    String(guardMessage(made, 'write', managed)).slice(0, 200),
  )
  const snapshot = made.routes.get('/rosetta-harness/state')
  check('the status route is registered', Boolean(snapshot))
}

// ---------------------------------------------------------------------------
// 5. The real bridge, with a genuinely corrupt store
// ---------------------------------------------------------------------------

{
  const broken = join(root, 'broken-workspace')
  mkdirSync(join(broken, '.rosetta'), { recursive: true })
  // A real, damaged SQLite store: exactly the "损坏库" case.
  writeFileSync(join(broken, '.rosetta', 'harness-sessions.sqlite3'), Buffer.alloc(4096, 7))
  const made = startPlugin({
    spec: { status: [{ sessions: [] }] },
    workspaceRoots: [broken],
  })
  // Point this instance at the REAL bridge so the failure comes from the store.
  const real = makeContext()
  module.apply(real.ctx, {
    pythonExecutable: PYTHON,
    workspaceRoots: [broken],
    stateFile: join(broken, '.rosetta', 'harness-sessions.sqlite3'),
  })
  await new Promise((done) => setTimeout(done, 1200))
  const target = join(broken, 'could-be-managed.typ')
  check(
    'a corrupt store leaves the fence refusing inside the root (real bridge)',
    guardDenies(real, 'write', target),
    String(guardMessage(real, 'write', target)).slice(0, 260),
  )
  check(
    'the corrupt-store refusal names the bridge as the reason',
    /STORE_CORRUPT|corrupt|not a database/i.test(String(guardMessage(real, 'write', target))),
    String(guardMessage(real, 'write', target)).slice(0, 260),
  )
  void made
}

// ---------------------------------------------------------------------------
// 6. The ready fence still behaves exactly as before
// ---------------------------------------------------------------------------

{
  const managed = join(workspace, 'normal.typ')
  const made = startPlugin({
    spec: {
      status: [{ delayMs: 0, sessions: [session('live', managed)] }],
      default: { ok: true },
    },
  })
  await new Promise((done) => setTimeout(done, 700))
  check('a ready fence denies the managed path', guardDenies(made, 'write', managed))
  check('a ready fence names the session in the refusal', /live/.test(String(guardMessage(made, 'write', managed))))
  check(
    'a ready fence allows an unmanaged path in the same root',
    !guardDenies(made, 'write', join(workspace, 'other.typ')),
  )
  check('a ready fence allows a non-mutating tool', !guardDenies(made, 'read', managed))
}

// --- summary --------------------------------------------------------------

delete process.env.ROSETTA_STUB_SPEC
delete process.env.ROSETTA_STUB_COUNTER
rmSync(root, { recursive: true, force: true })

const failures = results.filter((entry) => !entry.ok)
console.log('\n' + '='.repeat(72))
console.log(`${results.length - failures.length}/${results.length} checks passed`)
for (const failure of failures) console.log(`  FAILED: ${failure.label}\n          ${failure.detail}`)
process.exit(failures.length ? 1 : 0)

/**
 * Client-half regression test for the Rosetta status strip.
 *
 * The strip is a React slot component, so this test loads `client.js` with a
 * minimal React shim, captures the component registered for
 * `conversation.composer.dock`, and renders it with controlled Host snapshots.
 * It exercises the two properties that regressed in the real Harness demo:
 *
 *   1. a chat-scoped snapshot never presents another chat's session as this
 *      chat's work, and a fallback without a chat id says so explicitly while
 *      disabling Preview/Deliver/Release;
 *   2. a pass-through-only candidate preview is not rendered as "preview ready"
 *      and its Deliver button is disabled.
 *
 * Run: node tests/client_status_test.mjs
 */
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const CLIENT = resolve(HERE, '..', 'client.js')

const results = []
function check(label, condition, detail = '') {
  results.push({ label, ok: Boolean(condition), detail })
  console.log(`[${condition ? 'PASS' : 'FAIL'}] ${label}${condition || !detail ? '' : ` :: ${detail}`}`)
}

let stateQueue = []
let stateUpdates = []
const React = {
  createElement(type, props, ...children) {
    return { type, props: { ...(props || {}), children } }
  },
  useState(initial) {
    const value = stateQueue.length
      ? stateQueue.shift()
      : typeof initial === 'function'
        ? initial()
        : initial
    return [value, (next) => stateUpdates.push(next)]
  },
  useEffect() {},
  useRef(initial) {
    return { current: initial }
  },
  memo(component) {
    return component
  },
}

// The strip picks Chinese from the browser locale. Keep this test's assertions
// stable by pinning an English locale before the module reads `navigator`.
Object.defineProperty(globalThis, 'navigator', {
  value: { language: 'en-US' },
  configurable: true,
})

let loaded = null
globalThis.window = {
  __ModuleLoader__: {
    load({ factory }) {
      loaded = factory((id) => {
        if (id === 'react') return React
        throw new Error(`unexpected client dependency ${id}`)
      })
    },
  },
}

await import(`file:///${CLIENT.replace(/\\/g, '/')}`)
if (!loaded) throw new Error('client.js did not call window.__ModuleLoader__.load')

let StatusComponent = null
loaded.apply({
  slots: {
    inject(_name, callback) {
      return callback()
    },
    register(_spec, component) {
      StatusComponent = component
      return () => {}
    },
  },
})
if (typeof StatusComponent !== 'function') throw new Error('no status component registered')

function walk(node, out) {
  if (node === null || node === undefined || node === false || node === true) return
  if (Array.isArray(node)) {
    for (const child of node) walk(child, out)
    return
  }
  if (typeof node === 'string' || typeof node === 'number') {
    out.texts.push(String(node))
    return
  }
  if (typeof node.type === 'function') {
    walk(node.type(node.props), out)
    return
  }
  if (node.type === 'button') out.buttons.push(node.props)
  if (node.type === 'select') out.selects.push(node.props)
  walk(node.props && node.props.children, out)
}

function render(
  props,
  snapshot,
  {
    open = true,
    busy = false,
    notice = null,
    others = { open: false, sessions: null, error: null },
    selected = '',
    pendingConfirm = null,
    fullView = false,
  } = {},
) {
  stateUpdates = []
  // Hook order: RosettaStatus's open/busy/actionNotice/others/selected/
  // pendingConfirm/fullView, then useSnapshot's snapshot/error/revision.
  stateQueue = [open, busy, notice, others, selected, pendingConfirm, fullView, snapshot, null, 0]
  const tree = StatusComponent(props)
  const out = { texts: [], buttons: [], selects: [] }
  walk(tree, out)
  const actions = out.buttons.filter((entry) =>
    (entry.children || []).some((child) => ['Preview', 'Deliver', 'Release'].includes(child)),
  )
  return {
    text: out.texts.join(' | '),
    buttons: out.buttons,
    selects: out.selects,
    actions,
    allActionsDisabled:
      actions.length > 0 && actions.every((button) => button.disabled === true),
  }
}

function button(result, label) {
  return result.buttons.find((entry) => (entry.children || []).includes(label))
}

const baseSession = {
  session: 'ros-1',
  pair: 'PRQL / SQL',
  targetPath: 'C:\\workspace\\orders.prql',
  ownerSessionId: 'chat-A',
  dirty: false,
  hasConvertibleContent: true,
  viewEdited: false,
  previewCandidate: false,
  hasPreview: false,
  active: true,
  targetDrift: { drifted: false },
  projectionMetadata: {},
}

// An unchanged excerpt does not prove the remainder of a large file is unchanged.
{
  const excerpt = 'a'.repeat(4000)
  const session = { ...baseSession, dirty: true, viewEdited: true, previewCandidate: true,
    hasPreview: true, targetPreview: excerpt, preview: excerpt }
  const result = render({ sessionId: 'chat-A' },
    { ok: true, chatScoped: true, otherSessionCount: 0, sessions: [session] })
  check('identical truncated excerpts are not described as a complete unchanged file',
    result.text.includes('No line changes in the displayed portion') &&
    result.text.includes('first 4000 characters'), result.text.slice(0, 500))
}

// 2b. A collapsed strip has metadata only. Opening the Deliver confirmation
// must fetch the content for this chat, bind it to the current hashes, and show
// the target diff before the user can confirm.
{
  const metadata = {
    ...baseSession,
    dirty: true,
    viewEdited: true,
    previewCandidate: true,
    hasPreview: true,
    previewSha256: 'a'.repeat(64),
    viewSha256: 'b'.repeat(64),
    targetSha256: 'c'.repeat(64),
    // Deliberately no preview body: the collapsed status poll is metadata-only.
  }
  const content = {
    ...metadata,
    preview: 'SELECT new',
    targetPreview: 'SELECT old',
  }
  const originalFetch = globalThis.fetch
  let requestedUrl = ''
  globalThis.fetch = async (url) => {
    requestedUrl = String(url)
    return {
      status: 200,
      ok: true,
      json: async () => ({
        ok: true,
        chatScoped: true,
        contentIncluded: true,
        sessions: [content],
      }),
    }
  }
  const collapsed = render(
    { sessionId: 'chat-A' },
    { ok: true, chatScoped: true, otherSessionCount: 0, sessions: [metadata] },
    { open: false },
  )
  button(collapsed, 'Deliver')?.onClick()
  await new Promise((resolve) => setTimeout(resolve, 0))
  globalThis.fetch = originalFetch
  const pending = stateUpdates.find((value) => value && value.action === 'commit')
  check(
    'Deliver fetches preview content only for the owning chat',
    requestedUrl.includes('chat=chat-A')
      && requestedUrl.includes('content=1')
      && requestedUrl.includes('full=1')
      && pending?.entry?.session === metadata.session,
    `${requestedUrl} :: ${JSON.stringify(pending)}`,
  )
  check(
    'the fetched preview remains bound to the metadata hashes',
    pending?.expected?.previewSha256 === metadata.previewSha256
      && pending?.expected?.viewSha256 === metadata.viewSha256
      && pending?.expected?.targetSha256 === metadata.targetSha256,
    JSON.stringify(pending?.expected),
  )
  const hydratedConfirm = render(
    { sessionId: 'chat-A' },
    { ok: true, chatScoped: true, otherSessionCount: 0, sessions: [metadata] },
    { open: false, pendingConfirm: pending },
  )
  check(
    'a collapsed-strip confirmation shows the actual target-file diff',
    hydratedConfirm.text.includes('- SELECT old')
      && hydratedConfirm.text.includes('+ SELECT new')
      && hydratedConfirm.text.includes('A converter may normalize formatting'),
    hydratedConfirm.text,
  )
  const longPrefix = Array.from({ length: 500 }, (_, i) => `line ${i}`).join('\n')
  const longEntry = { ...content, targetPreview: longPrefix + '\nold ending',
    preview: longPrefix + '\nnew ending' }
  const longConfirm = render(
    { sessionId: 'chat-A' },
    { ok: true, chatScoped: true, otherSessionCount: 0, sessions: [metadata] },
    { open: false, pendingConfirm: { action: 'commit', entry: longEntry, expected: pending.expected } },
  )
  check('full confirmation compares changes after 4000 characters without a truncation warning',
    longConfirm.text.includes('- old ending') && longConfirm.text.includes('+ new ending') &&
      !longConfirm.text.includes('first 4000 characters'), longConfirm.text.slice(-700))
  const hugeBefore = Array.from({ length: 800 }, (_, i) => `old line ${i}`).join('\n')
  const hugeAfter = Array.from({ length: 800 }, (_, i) => `new line ${i}`).join('\n')
  const hugeConfirm = render(
    { sessionId: 'chat-A' },
    { ok: true, chatScoped: true, otherSessionCount: 0, sessions: [metadata] },
    { open: false, pendingConfirm: { action: 'commit',
      entry: { ...content, targetPreview: hugeBefore, preview: hugeAfter }, expected: pending.expected } },
  )
  check('very large diffs report omitted rows and accurate totals',
    hugeConfirm.text.includes('Many diff lines are omitted') && hugeConfirm.text.includes('+800 / -800 lines') &&
      hugeConfirm.text.includes('old line 0') && hugeConfirm.text.includes('new line 799') &&
      !hugeConfirm.text.includes('old line 500'), hugeConfirm.text.slice(0, 500))
  const oneSidedLarge = Array.from({ length: 12_000 }, (_, i) => `removed ${i}`).join('\n')
  const oneSidedConfirm = render(
    { sessionId: 'chat-A' },
    { ok: true, chatScoped: true, otherSessionCount: 0, sessions: [metadata] },
    { open: false, pendingConfirm: { action: 'commit', entry: { ...content,
      targetPreview: oneSidedLarge, preview: 'replacement' }, expected: pending.expected } },
  )
  check('one-sided large changes use bounded rendering and exact replacement counts',
    oneSidedConfirm.text.includes('Large changed block shown as replaced') &&
      oneSidedConfirm.text.includes('+1 / -12000 lines') &&
      oneSidedConfirm.text.includes('removed 0') && oneSidedConfirm.text.includes('removed 11999') &&
      oneSidedConfirm.text.includes('+ replacement'), oneSidedConfirm.text.slice(0, 700))
  const sharedLines = Array.from({ length: 60_000 }, (_, i) => `shared ${i}`)
  const largeStableBefore = [...sharedLines.slice(0, 30_000), 'old middle', ...sharedLines.slice(30_000)].join('\n')
  const largeStableAfter = [...sharedLines.slice(0, 30_000), 'new middle', ...sharedLines.slice(30_000)].join('\n')
  const largeStableConfirm = render(
    { sessionId: 'chat-A' },
    { ok: true, chatScoped: true, otherSessionCount: 0, sessions: [metadata] },
    { open: false, pendingConfirm: { action: 'commit', entry: { ...content,
      targetPreview: largeStableBefore, preview: largeStableAfter }, expected: pending.expected } },
  )
  check('a localized edit in a 60001-line file does not materialize the unchanged file as diff rows',
    largeStableConfirm.text.includes('- old middle') && largeStableConfirm.text.includes('+ new middle') &&
      largeStableConfirm.text.includes('shared 29999') && !largeStableConfirm.text.includes('shared 1000') &&
      !largeStableConfirm.text.includes('Many diff lines are omitted'), largeStableConfirm.text.slice(0, 700))
  const denseBefore = 'row\n'.repeat(100_000) + 'old final row'
  const denseAfter = 'row\n'.repeat(100_000) + 'new final row'
  const denseConfirm = render(
    { sessionId: 'chat-A' },
    { ok: true, chatScoped: true, otherSessionCount: 0, sessions: [metadata] },
    { open: false, pendingConfirm: { action: 'commit', entry: { ...content,
      targetPreview: denseBefore, preview: denseAfter }, expected: pending.expected } },
  )
  check('a 100001-line file uses a bounded diff and exposes both complete texts for inspection',
    denseConfirm.text.includes('A detailed line diff is omitted for files over 100,000 lines') &&
      denseConfirm.text.includes(denseBefore) && denseConfirm.text.includes(denseAfter) &&
      denseConfirm.buttons.some((item) => item['aria-label'] === 'Copy: Target view (last accepted)') &&
      denseConfirm.buttons.some((item) => item['aria-label'] === 'Copy: Delivery text'),
    `complete target=${denseConfirm.text.includes(denseBefore)}, complete delivery=${denseConfirm.text.includes(denseAfter)}, buttons=${denseConfirm.buttons.length}`)
  const lineEndingConfirm = render(
    { sessionId: 'chat-A' },
    { ok: true, chatScoped: true, otherSessionCount: 0, sessions: [metadata] },
    { open: false, pendingConfirm: { action: 'commit', entry: { ...content,
      targetPreview: 'alpha\r\nbeta\r\n', preview: 'alpha\nbeta\n' }, expected: pending.expected } },
  )
  check('line-ending-only changes are disclosed even when line content is identical',
    lineEndingConfirm.text.includes('No line changes') &&
      lineEndingConfirm.text.includes('Line-ending style differs'), lineEndingConfirm.text.slice(0, 600))
  const finalNewlineConfirm = render(
    { sessionId: 'chat-A' },
    { ok: true, chatScoped: true, otherSessionCount: 0, sessions: [metadata] },
    { open: false, pendingConfirm: { action: 'commit', entry: { ...content,
      targetPreview: 'alpha\nbeta\n', preview: 'alpha\nbeta' }, expected: pending.expected } },
  )
  check('a missing final newline is disclosed even when line content is identical',
    finalNewlineConfirm.text.includes('No line changes') &&
      finalNewlineConfirm.text.includes('The final newline differs'), finalNewlineConfirm.text.slice(0, 600))
  const longLineBefore = 'prefix-' + 'x'.repeat(5000) + '-old-end'
  const longLineAfter = 'prefix-' + 'y'.repeat(5000) + '-new-end'
  const longLineConfirm = render(
    { sessionId: 'chat-A' },
    { ok: true, chatScoped: true, otherSessionCount: 0, sessions: [metadata] },
    { open: false, pendingConfirm: { action: 'commit',
      entry: { ...content, targetPreview: longLineBefore, preview: longLineAfter }, expected: pending.expected } },
  )
  check('extremely long lines are shortened with an explicit warning',
    longLineConfirm.text.includes('Long diff lines are shortened') &&
      longLineConfirm.text.includes('-old-end') && longLineConfirm.text.includes('-new-end') &&
      !longLineConfirm.text.includes('x'.repeat(3000)), longLineConfirm.text.slice(0, 500))
}

// A changed preview or target between the metadata poll and content fetch must
// not open a confirmation for a candidate different from the one clicked.
{
  const metadata = {
    ...baseSession,
    dirty: true,
    viewEdited: true,
    previewCandidate: true,
    hasPreview: true,
    previewSha256: 'a'.repeat(64),
    viewSha256: 'b'.repeat(64),
    targetSha256: 'c'.repeat(64),
  }
  const changed = {
    ...metadata,
    previewSha256: 'd'.repeat(64),
    preview: 'SELECT changed',
    targetPreview: 'SELECT old',
  }
  const originalFetch = globalThis.fetch
  globalThis.fetch = async () => ({
    status: 200,
    ok: true,
    json: async () => ({
      ok: true,
      chatScoped: true,
      contentIncluded: true,
      sessions: [changed],
    }),
  })
  const collapsed = render(
    { sessionId: 'chat-A' },
    { ok: true, chatScoped: true, otherSessionCount: 0, sessions: [metadata] },
    { open: false },
  )
  button(collapsed, 'Deliver')?.onClick()
  await new Promise((resolve) => setTimeout(resolve, 0))
  globalThis.fetch = originalFetch
  check(
    'a changed candidate is refused before the delivery confirmation opens',
    !stateUpdates.some((value) => value && value.action === 'commit')
      && stateUpdates.some((value) => value?.tone === 'warn'
        && value.text.includes('preview or target changed')),
    JSON.stringify(stateUpdates),
  )
}


// 1. Current chat, all-pass-through candidate preview.
{
  const session = {
    ...baseSession,
    pair: 'Bash / Nushell (experimental)',
    targetPath: 'C:\\workspace\\request_summary.nu',
    hasConvertibleContent: false,
    viewEdited: false,
    previewCandidate: true,
    hasPreview: false,
  }
  const result = render(
    { sessionId: 'chat-A' },
    { ok: true, chatScoped: true, otherSessionCount: 0, sessions: [session] },
  )
  check(
    'pass-through preview is not called "preview ready"',
    !result.text.includes('preview ready'),
    result.text,
  )
  check(
    'pass-through candidate is labelled as a candidate preview',
    result.text.includes('candidate preview'),
    result.text,
  )
  check('pass-through view is labelled pass-through only', result.text.includes('pass-through only'))
  check('Deliver is disabled without convertible content', button(result, 'Deliver')?.disabled === true)
}

// 2. Current chat, an ordinary edited candidate: Deliver stays available.
{
  const session = {
    ...baseSession,
    dirty: true,
    viewEdited: true,
    previewCandidate: true,
    hasPreview: true,
    targetPreview: 'SELECT old',
    preview: 'SELECT new',
    previewSha256: 'a'.repeat(64),
    viewSha256: 'b'.repeat(64),
    targetSha256: 'c'.repeat(64),
  }
  const result = render(
    { sessionId: 'chat-A' },
    { ok: true, chatScoped: true, otherSessionCount: 0, sessions: [session] },
  )
  check(
    'an edited candidate is still not described as verified',
    result.text.includes('candidate preview')
      && result.text.includes('edited - request outcome not checked')
      && !result.text.includes('preview ready'),
    result.text,
  )
  check(
    'Deliver stays enabled for an edited candidate in the current chat',
    button(result, 'Deliver')?.disabled === false,
  )
  check('a manual refresh button is available', button(result, '↻')?.type === 'button')
  check('an open-file entry is available', button(result, '＋')?.type === 'button')
  check(
    'a candidate offers discard-preview without closing the session',
    button(result, 'Discard preview')?.disabled === false,
    JSON.stringify(result.buttons.filter((entry) => (entry.children || []).includes('Discard preview'))),
  )
  check(
    'the candidate diff shows removed and added lines',
    result.text.includes('Changes vs last accepted')
      && result.text.includes('- SELECT old')
      && result.text.includes('+ SELECT new')
      && result.text.includes('+1 / -1 lines'),
    result.text,
  )
  const restoreResult = render(
    { sessionId: 'chat-A' },
    {
      ok: true,
      chatScoped: true,
      otherSessionCount: 0,
      sessions: [{ ...baseSession, restoreAvailable: true }],
    },
  )
  check(
    'details offer loading the complete view instead of the 4000-char excerpt',
    button(result, 'Load full view')?.type === 'button',
    JSON.stringify(result.buttons.filter((entry) => (entry.children || []).includes('Load full view'))),
  )
  check(
    'a delivered session offers restoring the previous version',
    button(restoreResult, 'Restore previous')?.disabled === false,
    JSON.stringify(
      restoreResult.buttons.filter((entry) => (entry.children || []).includes('Restore previous')),
    ),
  )
  const confirming = render(
    { sessionId: 'chat-A' },
    { ok: true, chatScoped: true, otherSessionCount: 0, sessions: [session] },
    {
      pendingConfirm: {
        action: 'commit',
        entry: session,
        expected: {
          previewSha256: session.previewSha256,
          viewSha256: session.viewSha256,
          targetSha256: session.targetSha256,
        },
      },
    },
  )
  check(
    'delivery confirmation is inline rather than a window dialog',
    confirming.text.includes('Deliver this candidate?')
      && confirming.text.includes('has not checked whether the requested edit was fulfilled')
      && button(confirming, 'Confirm delivery')?.type === 'button'
      && button(confirming, 'Cancel')?.type === 'button',
    confirming.text,
  )
  check(
    'delivery confirmation shows target-file diff lines and explains format normalization limits',
    confirming.text.includes('Target-file changes before delivery')
      && confirming.text.includes('- SELECT old')
      && confirming.text.includes('+ SELECT new')
      && confirming.text.includes('A converter may normalize formatting')
      && confirming.text.includes('does not verify that the requested semantics are correct'),
    confirming.text,
  )
  const originalFetch = globalThis.fetch
  let commitBody = null
  globalThis.fetch = async (_url, options = {}) => {
    commitBody = JSON.parse(options.body || '{}')
    return { status: 200, ok: true, json: async () => ({ ok: true }) }
  }
  button(confirming, 'Confirm delivery')?.onClick()
  await new Promise((resolve) => setTimeout(resolve, 0))
  globalThis.fetch = originalFetch
  check(
    'the confirmation request carries the preview, view and target digests',
    commitBody?.action === 'commit'
      && commitBody.expectedPreviewSha256 === session.previewSha256
      && commitBody.expectedViewSha256 === session.viewSha256
      && commitBody.expectedTargetSha256 === session.targetSha256,
    JSON.stringify(commitBody),
  )
  const releasingDirty = render(
    { sessionId: 'chat-A' },
    { ok: true, chatScoped: true, otherSessionCount: 0, sessions: [session] },
    { pendingConfirm: { action: 'close', entry: session } },
  )
  check(
    'dirty release uses the same inline confirmation',
    releasingDirty.text.includes('Release and discard the draft?')
      && button(releasingDirty, 'Confirm release')?.type === 'button',
    releasingDirty.text,
  )
  const releasingOther = render(
    { sessionId: 'chat-A' },
    { ok: true, chatScoped: true, otherSessionCount: 1, sessions: [session] },
    {
      pendingConfirm: {
        action: 'release-other',
        entry: {
          ...baseSession,
          session: 'ros-other',
          targetPath: 'C:\\workspace\\other.prql',
        },
      },
    },
  )
  check(
    'other-session release uses the same inline confirmation',
    releasingOther.text.includes('Release this other session?')
      && button(releasingOther, 'Confirm release')?.type === 'button',
    releasingOther.text,
  )
}

// 3. "+N other" is a real button, and opening it shows read-only sessions.
{
  const mine = {
    ...baseSession,
    dirty: true,
    viewEdited: true,
    previewCandidate: true,
    hasPreview: true,
  }
  const foreign = {
    ...baseSession,
    session: 'ros-other',
    ownerSessionId: 'chat-B',
    targetPath: 'C:\\workspace\\other.prql',
    dirty: true,
    viewEdited: true,
    previewCandidate: true,
    hasPreview: true,
  }
  const result = render(
    { sessionId: 'chat-A' },
    { ok: true, chatScoped: true, otherSessionCount: 1, sessions: [mine] },
    { others: { open: true, sessions: [foreign], error: null } },
  )
  check(
    '+N other is a clickable button',
    button(result, '+1 other')?.type === 'button'
      && button(result, '+1 other')?.['aria-expanded'] === true,
    JSON.stringify(result.buttons.filter((entry) => (entry.children || []).includes('+1 other'))),
  )
  check(
    'the other-sessions panel is visible',
    result.text.includes('Other sessions') && result.text.includes('chat-B'),
    result.text,
  )
  check(
    'the other-session note explains the read-only/open-or-release choice',
    result.text.includes('Open in this chat to edit, or release this session to discard it.'),
    result.text,
  )
  check(
    'an other-chat session offers "Open in this chat"',
    button(result, 'Open in this chat')?.disabled === false,
    JSON.stringify(result.buttons.filter((entry) => (entry.children || []).includes('Open in this chat'))),
  )
  check(
    'an other-chat session offers an explicit release',
    button(result, 'Release this session')?.disabled === false,
    JSON.stringify(
      result.buttons.filter((entry) => (entry.children || []).includes('Release this session')),
    ),
  )
}

// 4. Pair availability is visible without opening a file first.
{
  const result = render(
    { sessionId: 'chat-A' },
    {
      ok: true,
      chatScoped: true,
      otherSessionCount: 0,
      sessions: [baseSession],
      pairs: [
        {
          name: 'PRQL / SQL',
          available: true,
          missingRequired: [],
          missingOptional: [],
        },
        {
          name: 'Verilog / SystemVerilog',
          available: false,
          missingRequired: ['sv2v'],
          missingOptional: [],
        },
      ],
    },
  )
  check(
    'pair issues appear as a compact summary',
    result.text.includes('Pairs 1/2'),
    result.text,
  )
  check(
    'pair details name the missing converter',
    result.text.includes('Tool pairs') && result.text.includes('sv2v'),
    result.text,
  )
}

// 5. Agent tool activity is visible before the next state poll completes.
{
  const result = render(
    { sessionId: 'chat-A' },
    {
      ok: true,
      chatScoped: true,
      otherSessionCount: 0,
      sessions: [baseSession],
      activity: { command: 'preview', startedAt: Date.now() },
    },
  )
  check(
    'agent activity appears in the strip',
    result.text.includes('Agent: preparing preview...'),
    result.text,
  )
}

// 5b. An idle dirty draft must not read as finished/committed, and the
// confirmation for delivering it must say that the fence is gone while the
// draft is still here.
{
  const idleDraft = { ...baseSession, active: false, dirty: true }
  const result = render(
    { sessionId: 'chat-A' },
    { ok: true, chatScoped: true, otherSessionCount: 0, sessions: [idleDraft] },
  )
  check(
    'an idle session says the write fence was released while the draft is kept',
    result.text.includes('idle — write fence released, draft kept')
      && result.text.includes('The Harness write/edit fence was released after inactivity')
      && result.text.includes('Deliver still refuses if the target changed on disk'),
    result.text,
  )
  const idleCandidate = {
    ...idleDraft,
    viewEdited: true,
    previewCandidate: true,
    hasPreview: true,
    targetPreview: 'SELECT old',
    preview: 'SELECT new',
  }
  const candidateResult = render(
    { sessionId: 'chat-A' },
    { ok: true, chatScoped: true, otherSessionCount: 0, sessions: [idleCandidate] },
  )
  const actionsAt = candidateResult.text.indexOf('Actions')
  const candidateAt = candidateResult.text.indexOf('Candidate delivery (not written)')
  check(
    'idle-session actions appear before long preview contents',
    actionsAt >= 0 && candidateAt > actionsAt,
    candidateResult.text,
  )
  check(
    'idle-session preview and deliver remain available near the top of details',
    button(candidateResult, 'Preview')?.disabled === false
      && button(candidateResult, 'Deliver')?.disabled === false,
    JSON.stringify(candidateResult.actions.map(({ children, disabled }) => ({ children, disabled }))),
  )
  const confirming = render(
    { sessionId: 'chat-A' },
    { ok: true, chatScoped: true, otherSessionCount: 0, sessions: [idleDraft] },
    { pendingConfirm: { action: 'commit', entry: idleDraft, expected: {} } },
  )
  check(
    'the delivery confirmation for an idle session explains the released fence',
    confirming.text.includes('This session is idle, so the Harness write/edit fence is released')
      && confirming.text.includes('delivery checks the target hash'),
    confirming.text,
  )
}

// 5c. The strip's Details expose the Host generation, so a user can compare it
// with the digest check_activation.ps1 reports.
{
  const digest = 'ab12cd34ef56'.repeat(5) + 'abcd'
  const result = render(
    { sessionId: 'chat-A' },
    {
      ok: true,
      chatScoped: true,
      otherSessionCount: 0,
      generation: { digest, files: {} },
      sessions: [baseSession],
    },
  )
  check(
    'details show the short Host bundle digest',
    result.text.includes('Host bundle') && result.text.includes(digest.slice(0, 12)),
    result.text,
  )
}

// 6. Several sessions in one chat are visible as a count, not silently hidden.
{
  const second = {
    ...baseSession,
    session: 'ros-2',
    targetPath: 'C:\\workspace\\second.prql',
  }
  const result = render(
    { sessionId: 'chat-A' },
    {
      ok: true,
      chatScoped: true,
      otherSessionCount: 0,
      sessions: [baseSession, second],
    },
  )
  const select = result.selects[0]
  const options = (select && select.children) || []
  check(
    'multiple sessions in one chat get a switch selector',
    result.selects.length === 1 && options.length === 2,
    JSON.stringify(result.selects),
  )
  check(
    'the session selector shows the primary session and both choices',
    select?.value === baseSession.session
      && options.some((option) => option.props.value === baseSession.session)
      && options.some((option) => option.props.value === second.session),
    JSON.stringify(options),
  )
  const switched = render(
    { sessionId: 'chat-A' },
    {
      ok: true,
      chatScoped: true,
      otherSessionCount: 0,
      sessions: [baseSession, second],
    },
    { selected: second.session },
  )
  check(
    'selecting another session changes the primary status',
    switched.selects[0]?.value === second.session
      && switched.text.includes('second.prql'),
    switched.text,
  )
}

// 7. While an action is in flight, the strip says so and disables every button.
{
  const session = {
    ...baseSession,
    dirty: true,
    viewEdited: true,
    previewCandidate: true,
    hasPreview: true,
  }
  const result = render(
    { sessionId: 'chat-A' },
    { ok: true, chatScoped: true, otherSessionCount: 0, sessions: [session] },
    { busy: true },
  )
  check('busy state shows a working indicator', result.text.includes('Working...'), result.text)
  check(
    'busy state disables every action',
    result.actions.length > 0 && result.actions.every((entry) => entry.disabled === true),
    JSON.stringify(result.actions),
  )
}

// 4. Action feedback is rendered in the strip, not only in a thrown error.
{
  const result = render(
    { sessionId: 'chat-A' },
    { ok: true, chatScoped: true, otherSessionCount: 0, sessions: [baseSession] },
    {
      notice: {
        tone: 'warn',
        text: 'Candidate preview generated; nothing was written. Rosetta has not checked whether the requested change was fulfilled.',
      },
    },
  )
  check(
    'action feedback is visible after a preview',
    result.text.includes('Candidate preview generated; nothing was written.'),
    result.text,
  )
}

// 5. A fresh chat must not inherit another chat's session.
{
  const result = render(
    { sessionId: 'chat-new' },
    {
      ok: false,
      disabled: true,
      reason: 'No `workspaceRoots` configured. Waiting for the current chat.',
      sessions: [],
    },
  )
  check(
    'zero-config strip explains the next step instead of showing an HTTP error',
    result.text.includes('Ask the agent in this chat to open a supported file')
      && !result.text.includes('HTTP 503'),
    result.text,
  )
}

{
  const result = render(
    { sessionId: 'chat-new' },
    { ok: true, chatScoped: true, otherSessionCount: 2, sessions: [] },
  )
  check(
    'a fresh chat shows no Rosetta artifact instead of another chat preview',
    result.text.includes('No Rosetta artifact in this chat.'),
    result.text,
  )
  check(
    'the fresh chat reports that other sessions are hidden',
    result.text.includes('2 session(s) in this workspace belong to other chats and are hidden here.'),
    result.text,
  )
  check(
    'no delivery actions are offered for hidden sessions',
    result.actions.length === 0,
    JSON.stringify(result.actions),
  )
  check(
    'the hidden-session count is clickable from a fresh chat',
    button(result, '+2 other')?.type === 'button',
    JSON.stringify(result.buttons),
  )
  check(
    'a fresh chat offers the open-file entry',
    button(result, 'Open file')?.type === 'button'
      && button(result, 'Open file')?.disabled === false,
    JSON.stringify(result.buttons),
  )
}

// 4. No chat id: the fallback is explicit, workspace-scoped, and read-only.
{
  const session = {
    ...baseSession,
    ownerSessionId: 'chat-B',
    dirty: true,
    viewEdited: true,
    previewCandidate: true,
    hasPreview: true,
  }
  const result = render(
    {},
    { ok: true, chatScoped: false, otherSessionCount: 0, sessions: [session] },
  )
  check(
    'the no-chat fallback is labelled as this workspace\'s other sessions',
    result.text.includes("This workspace's other sessions (此工作区其他会话)"),
    result.text,
  )
  check(
    'the fallback explains that actions are disabled',
    result.text.includes('Preview, Deliver and Release are disabled'),
    result.text,
  )
  check(
    'the fallback disables every session action',
    result.allActionsDisabled,
    JSON.stringify(result.actions),
  )
}

const failures = results.filter((entry) => !entry.ok)
console.log('\n' + '='.repeat(72))
console.log(`${results.length - failures.length}/${results.length} checks passed`)
for (const failure of failures) console.log(`  FAILED: ${failure.label}\n          ${failure.detail}`)
process.exit(failures.length ? 1 : 0)

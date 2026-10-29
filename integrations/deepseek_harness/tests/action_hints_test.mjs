/**
 * Contract tests for user-facing recovery guidance in index.js.
 *
 * Run: node tests/action_hints_test.mjs
 */
import { ACTION_HINTS } from '../index.js'

const cases = [
  ['TARGET_NOT_FOUND', ['ros_open_artifact', 'ros_create_artifact', 'No file was created']],
  ['PARENT_MISSING', ['create the parent directory first', 'does not create directories']],
  ['EXTENSION_MISMATCH', ['target extension matches', 'ros_list_pairs']],
  ['ARTIFACT_TOO_LARGE', ['2,000,000 UTF-8 bytes', '2 MB']],
  ['TARGET_EXISTS', ['never overwrites', 'ros_open_artifact']],
  ['INITIAL_VIEW_PASSTHROUGH', ['no existing target-native block', 'create the native target file first']],
  ['CREATE_CONTENT_AMBIGUOUS', ['initialAgentView', 'initialTargetContent', 'not both']],
  ['NOT_UTF8', ['UTF-8', 'reopen']],
  ['PATH_REQUIRED', ['artifact path', 'workspace root']],
  ['PATH_IS_PLUGIN_STATE', ['.rosetta', 'plugin state']],
  ['WORKSPACE_ROOT_NOT_ABSOLUTE', ['absolute directory path']],
  ['WORKSPACE_ROOT_MISSING', ['existing directory', 'workspaceRoots']],
  ['SESSION_ID_REQUIRED', ['session id returned']],
  ['AMBIGUOUS_SESSION', ['full session id']],
  ['INVALID_ENCODING', ['valid UTF-8', 'did not run']],
  ['PROJECTION_FAILED', ['ros_doctor', 'converter', 'construct named in the error']],
  ['PROJECTION_EMPTY', ['contains readable content', 'did not open a session']],
  ['PROJECTION_MALFORMED', ['pass-through markers', 'reopening']],
  ['DELIVERY_FAILED', ['ros_doctor', 'backend', 'preview again']],
  ['HISTORY_UNAVAILABLE', ['not written', 'commit-history', 'free space']],
  ['RESTORE_UNAVAILABLE', ['no restore occurred', 'backup or version control']],
  ['RESTORE_TARGET_CHANGED', ['newer content', 'no restore occurred']],
  ['CONFIRMATION_STALE', ['changed after confirmation', 'Nothing was written']],
  ['CONTENT_REQUIRED', ['complete familiar view', 'content field']],
]

const failures = []
let passed = 0
for (const [code, requiredFragments] of cases) {
  const hint = ACTION_HINTS[code]
  const missing = requiredFragments.filter((fragment) => !hint?.includes(fragment))
  if (typeof hint !== 'string' || missing.length) {
    failures.push(`${code}: missing hint or fragments ${missing.join(', ')}`)
    console.error(`[FAIL] ${code}: ${missing.join(', ') || 'no hint'}`)
  } else {
    passed += 1
    console.log(`[PASS] ${code}: code-specific next action`)
  }
}

// These failures must not be collapsed into one generic "unsupported syntax"
// message: their remedies differ and some explicitly guarantee no write.
for (const [left, right] of [
  ['TARGET_NOT_FOUND', 'PARENT_MISSING'],
  ['PROJECTION_FAILED', 'PROJECTION_EMPTY'],
  ['RESTORE_UNAVAILABLE', 'RESTORE_TARGET_CHANGED'],
  ['HISTORY_UNAVAILABLE', 'CONFIRMATION_STALE'],
  ['TARGET_EXISTS', 'EXTENSION_MISMATCH'],
]) {
  if (ACTION_HINTS[left] === ACTION_HINTS[right]) {
    failures.push(`${left} and ${right} unexpectedly share a generic hint`)
  }
}

console.log(`\n${passed}/${cases.length} required action-hint cases passed`)
if (failures.length) {
  for (const failure of failures) console.error(`  ${failure}`)
  process.exitCode = 1
}

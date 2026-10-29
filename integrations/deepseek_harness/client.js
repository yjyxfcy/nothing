/**
 * Rosetta compatibility layer — Client half.
 *
 * Renders one ambient strip below the composer that shows, live, what the
 * Rosetta Host plugin is doing: the target artifact, the pair, whether the
 * familiar view has unsaved edits, whether the target changed on disk, and
 * whether a delivery preview is pending.
 *
 * The strip carries no logic of its own. It polls the Host route
 * `/rosetta-harness/state`, which reads the same JSON store the tools write, so
 * what the user sees cannot drift from what the agent is editing.
 *
 * Styling uses only `--dsw-alias-*` theme tokens, so a renamed token degrades
 * the appearance rather than breaking the strip, and light/dark switching is
 * handled by the host.
 */
window.__ModuleLoader__.load({
  id: '@local/dsh-rosetta-compat',
  factory(require) {
    const React = require('react')
    const h = React.createElement

    const STATE_URL = '/rosetta-harness/state'
    const ACTION_URL = '/rosetta-harness/action'
    const POLL_MS = 2500

    // The Host UI is bilingual. Keep the English strings as the source of
    // truth because the automated client test runs with an English locale.
    const isZh =
      typeof navigator !== 'undefined' &&
      /^zh/i.test(String(navigator.language || ''))
    const tr = (en, zh) => (isZh ? zh : en)

    const ui = {
      brand: 'Rosetta',
      idle: tr('idle', '空闲'),
      ready: tr(
        'Ask the agent in this chat to open a .typ / .sv / .prql / .nu file; preview and deliver here.',
        '在当前聊天里让 Agent 打开 .typ / .sv / .prql / .nu 文件，然后在这里预览和交付。',
      ),
      noArtifact: tr('No Rosetta artifact in this chat.', '当前聊天没有 Rosetta 文件。'),
      workspaceNeeded: tr(
        'No workspace is configured yet. Ask the agent in this chat to open a supported file, or run setup.ps1 for this workspace.',
        '尚未配置工作区。请在当前聊天让 Agent 打开受支持的文件，或先为此工作区运行 setup.ps1。',
      ),
      finished: (n) =>
        tr(
          `${n} finished session${n === 1 ? '' : 's'} in this chat.`,
          `当前聊天有 ${n} 个已结束会话。`,
        ),
      hidden: (n) =>
        tr(
          `${n} session(s) in this workspace belong to other chats and are hidden here.`,
          `工作区另有 ${n} 个会话属于其他聊天，已在此隐藏。`,
        ),
      otherTitle: tr(
        "This workspace's other sessions (此工作区其他会话)",
        '此工作区其他会话',
      ),
      otherNotice: tr(
        'Chat ownership is unavailable in this status strip, so Preview, Deliver and Release are disabled for these sessions. Open the file in the current chat to act on it.',
        '此状态条无法识别当前聊天，因此这些会话的预览、交付、释放按钮均禁用。请在当前聊天中打开文件。',
      ),
      otherChip: (n) => tr(`+${n} other`, `+${n} 其他`),
      otherSessions: tr('Other sessions', '其他会话'),
      otherSessionsHint: tr(
        'Show sessions that belong to other chats; they are read-only here.',
        '查看属于其他聊天的会话；这里只读。',
      ),
      otherSessionsEmpty: tr(
        'No other sessions in this workspace.',
        '此工作区没有其他会话。',
      ),
      loading: tr('Loading...', '加载中…'),
      review: tr('Review', '查看'),
      hide: tr('Hide', '收起'),
      details: tr('Details', '详情'),
      hideDetails: tr('Hide details', '收起详情'),
      preview: tr('Preview', '预览'),
      deliver: tr('Deliver', '交付'),
      release: tr('Release', '释放'),
      openFile: tr('Open file', '打开文件'),
      openFileHint: tr(
        'Choose a .typ / .sv / .prql / .nu file from this computer.',
        '从本机选择一个 .typ / .sv / .prql / .nu 文件。',
      ),
      openFileNoBridge: tr(
        'The desktop path bridge is unavailable, so a picked file cannot become a Host path. Ask the agent to open it instead.',
        '桌面路径桥不可用，无法把所选文件变成 Host 路径；请让 Agent 打开它。',
      ),
      openFileNoChat: tr(
        'The current chat cannot be identified, so opening a file is disabled.',
        '无法识别当前聊天，打开文件已禁用。',
      ),
      openFileUnsupported: tr(
        'That file is not a supported Rosetta target (.typ / .sv / .prql / .nu).',
        '该文件不是受支持的 Rosetta 目标（.typ / .sv / .prql / .nu）。',
      ),
      openInThisChat: tr('Open in this chat', '在当前聊天打开'),
      releaseOther: tr('Release this session', '释放此会话'),
      confirmReleaseOther: (session, targetPath) =>
        tr(
          `Release session ${session} for ${targetPath}? Its unsaved draft will be discarded.`,
          `释放会话 ${session}（${targetPath}）？其未保存草稿会被丢弃。`,
        ),
      openHint: tr(
        'Open the file on disk in this chat. The other chat draft is not transferred.',
        '在当前聊天打开磁盘上的这个文件；另一个聊天的草稿不会转移。',
      ),
      otherOpenHint: tr(
        'Read-only here. Open in this chat to edit the file on disk.',
        '这里只读；在当前聊天打开后即可编辑磁盘文件。',
      ),
      otherManageHint: tr(
        'Read-only here. Open in this chat to edit, or release this session to discard it.',
        '这里只读；可在当前聊天打开后编辑，或释放此会话以丢弃它。',
      ),
      copy: tr('Copy', '复制'),
      dismiss: tr('Dismiss', '关闭'),
      working: tr('Working...', '处理中…'),
      refresh: tr('Refresh now', '立即刷新'),
      pairs: tr('Tool pairs', '工具对'),
      pairsChip: (ready, total) => tr(`Pairs ${ready}/${total}`, `工具对 ${ready}/${total}`),
      pairsUnavailable: tr('Pair status unavailable', '工具对状态不可用'),
      pairReady: tr('available', '可用'),
      pairDegraded: tr('degraded', '降级'),
      pairBlocked: tr('unavailable', '不可用'),
      diff: tr('Changes vs last accepted', '相对上次已接受的变化'),
      confirmTargetDiff: tr('Target-file changes before delivery', '交付前的目标文件变化'),
      confirmTargetDiffNote: tr(
        'Review the generated target text below. A converter may normalize formatting; this diff does not verify that the requested semantics are correct.',
        '请检查下方生成的目标文本。转换器可能会规范化格式；此差异不能验证请求的语义是否正确。',
      ),
      diffSummary: (adds, dels) => tr(`+${adds} / -${dels} lines`, `+${adds} / -${dels} 行`),
      diffEmpty: tr('No line changes', '没有行级变化'),
      diffEmptyTruncated: tr('No line changes in the displayed portion', '显示的部分没有行级变化'),
      diffLineEndings: tr(
        'Line-ending style differs (LF / CRLF / CR); the line diff normalizes line endings.',
        '换行符格式不同（LF / CRLF / CR）；行差异视图会统一换行符。',
      ),
      diffFinalNewline: tr(
        'The final newline differs; the line diff does not show it as a separate line.',
        '文件末尾换行符不同；行差异视图不会把它显示为单独一行。',
      ),
      diffTooManyLines: tr(
        'A detailed line diff is omitted for files over 100,000 lines to keep the UI responsive. Open or copy the complete target and delivery text below.',
        '文件超过 100,000 行，为保持界面响应速度，已省略逐行差异。请在下方展开或复制完整目标文本与交付文本。',
      ),
      diffRowsOmitted: tr('Many diff lines are omitted; review the complete files before delivery.', '差异过多，部分行未显示；交付前请查看完整文件。'),
      diffLongLines: tr('Long diff lines are shortened; review the complete files before delivery.', '差异中的长行已缩短；交付前请查看完整文件。'),
      diffLarge: tr(
        'Large changed block shown as replaced; context around it is still shown.',
        '大段变化以替换块显示，仍保留周边上下文。',
      ),
      loadFullView: tr('Load full view', '加载完整视图'),
      loadFullViewHint: tr(
        'Load the complete familiar view and target preview; the ambient status stays truncated.',
        '加载完整的熟悉视图和目标预览；常规状态请求仍保持截断。',
      ),
      fullViewLoaded: tr('Full view loaded.', '完整视图已加载。'),
      fenceScope: tr(
        'The write fence blocks Harness write/edit tools. Shell commands, scripts, and other applications are not blocked; Rosetta reports their changes as target drift.',
        '写入围栏只拦截 Harness 的 write/edit 工具；命令行、脚本和其他应用不会被拦截，Rosetta 会把它们造成的改动标为目标漂移。',
      ),
      diffTruncated: tr(
        'Large files are compared on the first 4000 characters; verify the rest in the full preview.',
        '大文件只比较前 4000 个字符；其余部分请在完整预览中确认。',
      ),
      switchSession: tr('Switch session', '切换会话'),
      switchSessionHint: tr(
        'Choose which Rosetta session is shown as the primary status.',
        '选择主状态显示哪个 Rosetta 会话。',
      ),
      agentWorking: (label) => tr(`Agent: ${label}...`, `Agent：${label}…`),
      activity: {
        open: tr('opening artifact', '正在打开工件'),
        create: tr('starting artifact', '正在创建工件'),
        read: tr('reading view', '正在读取视图'),
        write: tr('updating view', '正在更新视图'),
        preview: tr('preparing preview', '正在生成预览'),
        commit: tr('delivering', '正在交付'),
        close: tr('releasing session', '正在释放会话'),
        default: tr('working', '处理中'),
      },
      artifact: tr('Artifact', '文件'),
      delivery: tr('Delivery', '交付'),
      actions: tr('Actions', '操作'),
      target: tr('Target', '目标文件'),
      pair: tr('Pair', '配对'),
      session: tr('Session', '会话'),
      owner: tr('Chat', '所属聊天'),
      ownerThis: tr('this chat', '当前聊天'),
      ownerUnknown: tr('unknown', '未知'),
      protection: tr('File protection', '文件保护'),
      build: tr('Host bundle', '宿主构建'),
      released: tr('idle — no pending draft', '空闲 — 无待处理草稿'),
      idleDraft: tr(
        'idle — write fence released, draft kept',
        '空闲 — 写入围栏已释放，草稿仍保留',
      ),
      draftRetention: tr(
        'The Harness write/edit fence was released after inactivity. This draft stays in the local session store, and Deliver still refuses if the target changed on disk.',
        'Harness 的 write/edit 围栏因闲置而释放。草稿仍保存在本地会话存储中；若磁盘目标已变化，交付仍会被拒绝。',
      ),
      idleDeliverNote: tr(
        'This session is idle, so the Harness write/edit fence is released on this path. The draft is still stored here; delivery checks the target hash and refuses if the file changed.',
        '此会话已闲置，因此该路径上的 Harness write/edit 围栏已释放。草稿仍保存在这里；交付会检查目标哈希，文件已变化就拒绝写入。',
      ),
      convertible: tr('Convertible content', '可转换内容'),
      convertiblePresent: tr('present', '有'),
      convertibleNone: tr('none - pass-through only', '无 · 仅直通'),
      familiar: tr('Familiar view', '熟悉视图'),
      familiarDirty: tr('unsaved edits pending', '有未保存编辑'),
      familiarClean: tr('in sync', '已同步'),
      viewEdit: tr('View edit', '视图修改'),
      viewEdited: tr('changed since open (request outcome not checked)', '打开后已变化（未检查请求结果）'),
      viewUnchanged: tr('unchanged', '未变化'),
      targetDisk: tr('Target on disk', '磁盘文件'),
      targetChanged: (reason) => tr(`CHANGED - ${reason}`, `已变化 - ${reason}`),
      targetUnchanged: tr('unchanged since open', '打开后未变化'),
      previewPending: tr('Delivery preview', '交付预览'),
      previewNone: tr('none pending', '无待处理'),
      previewCandidate: tr(
        'candidate generated, not written; Rosetta has not checked whether the request was fulfilled',
        '已生成候选，尚未写入；Rosetta 未检查请求是否完成',
      ),
      previewCandidateNoConvertible: tr(
        'candidate generated, not written; no convertible content',
        '已生成候选，尚未写入；无可转换内容',
      ),
      backend: tr('Backend', '后端'),
      passthrough: (n, names) =>
        tr(
          `${n} target-only construct(s) preserved: ${names}`,
          `保留 ${n} 个目标格式专有结构：${names}`,
        ),
      targetView: tr('Target view (last accepted)', '目标视图（上次已接受）'),
      agentView: tr('Agent view (live)', 'Agent 视图（实时）'),
      deliveryText: tr('Delivery text', '待交付文本'),
      candidateDelivery: tr('Candidate delivery (not written)', '候选交付（未写入）'),
      statusSynced: tr('synced', '已同步'),
      statusEdited: tr('edited - request outcome not checked', '已编辑 · 未检查请求结果'),
      statusPassThrough: tr('pass-through only', '仅直通'),
      statusDrifted: tr('target changed', '目标已变'),
      statusCandidate: tr('candidate preview', '候选预览'),
      statusCandidateNoConvertible: tr(
        'candidate preview - no convertible content',
        '候选预览 · 无可转换内容',
      ),
      statusCandidateUnchanged: tr(
        'candidate preview - view unchanged',
        '候选预览 · 视图未变化',
      ),
      statusCandidateUnverified: tr(
        'candidate preview - Rosetta has not checked whether the request was fulfilled',
        '候选预览 · Rosetta 未检查请求是否完成',
      ),
      previewHint: tr(
        'Generate a candidate delivery without writing a file.',
        '生成候选交付，不写入文件。',
      ),
      previewHintNoConvertible: tr(
        'Generate a candidate preview; this view is pass-through only, so nothing is translated.',
        '生成候选预览；当前视图仅直通，不会发生翻译。',
      ),
      deliverHint: tr(
        'Write the previewed delivery to disk.',
        '把已预览的交付写入磁盘。',
      ),
      deliverFirst: tr('Generate a preview first.', '请先生成预览。'),
      deliverNoEdit: tr(
        'The familiar view has no edit yet, so there is nothing to deliver.',
        '熟悉视图还没有修改，没有可交付内容。',
      ),
      deliverNoConvertible: tr(
        'There is no convertible content, so Rosetta cannot translate an edit.',
        '没有可转换内容，Rosetta 无法翻译修改。',
      ),
      deliverDrift: tr(
        'The target changed on disk; review it before delivering.',
        '磁盘目标已变化，交付前请先查看。',
      ),
      scopeUnknown: tr(
        'disabled: this session does not belong to the current chat',
        '已禁用：此会话不属于当前聊天',
      ),
      scopeOther: tr(
        'disabled: belongs to another chat or has no recorded owner',
        '已禁用：属于其他聊天或没有记录归属',
      ),
      noticeOpen: tr(
        'Opened in this chat; the disk file is now owned by a new session here.',
        '已在当前聊天打开；磁盘文件现在由这里的新会话管理。',
      ),
      noticeReleaseOther: tr(
        'Other session released; its pending draft was discarded.',
        '其他会话已释放；其待处理草稿已被丢弃。',
      ),
      restoreLast: tr('Restore previous', '恢复上一版'),
      restoreHint: tr(
        'Restore the file bytes that preceded the last delivery, then close this session.',
        '恢复上次交付前的文件内容，然后关闭此会话。',
      ),
      confirmRestoreTitle: tr('Restore the previous version?', '恢复上一版？'),
      confirmRestoreHint: tr(
        'The current target will be overwritten with the bytes saved before the last delivery. The session will close.',
        '当前目标会被上次交付前保存的内容覆盖；此会话随后关闭。',
      ),
      confirmRestoreAction: tr('Confirm restore', '确认恢复'),
      noticeRestore: tr(
        'Previous target version restored; the session was closed.',
        '已恢复目标文件的上一版；会话已关闭。',
      ),
      discardPreview: tr('Discard preview', '丢弃预览'),
      discardPreviewHint: tr(
        'Drop the candidate delivery but keep the familiar-view edit.',
        '丢弃候选交付，但保留熟悉的视图编辑。',
      ),
      noticeDiscardPreview: tr(
        'Candidate delivery discarded; the familiar-view edit is still here.',
        '候选交付已丢弃；熟悉的视图编辑仍然保留。',
      ),
      confirmDeliverTitle: tr('Deliver this candidate?', '交付这个候选？'),
      confirmDeliverHint: tr(
        'This writes the candidate to disk. Rosetta has not checked whether the requested edit was fulfilled.',
        '这会把候选写入磁盘；Rosetta 未检查请求是否完成。',
      ),
      confirmReleaseDirtyTitle: tr('Release and discard the draft?', '释放并丢弃草稿？'),
      confirmReleaseDirtyHint: tr(
        'The pending familiar-view edit for this session will be discarded.',
        '此会话待处理的熟悉的视图编辑会被丢弃。',
      ),
      confirmReleaseOtherTitle: tr('Release this other session?', '释放这个其他会话？'),
      confirmDelivery: tr('Confirm delivery', '确认交付'),
      confirmDiffUnavailable: tr(
        'Could not load the current target-file diff. Refresh the status strip and try again; nothing was written.',
        '无法读取当前目标文件差异。请刷新状态面板后重试；文件未写入。',
      ),
      confirmDiffStale: tr(
        'The preview or target changed before confirmation opened. Refresh and review the current candidate; nothing was written.',
        '打开确认框前，预览或目标文件已发生变化。请刷新并重新检查当前候选；文件未写入。',
      ),
      confirmReleaseAction: tr('Confirm release', '确认释放'),
      cancel: tr('Cancel', '取消'),
      noticePreview: tr(
        'Candidate preview generated; nothing was written. Rosetta has not checked whether the requested change was fulfilled.',
        '已生成候选预览，尚未写入；Rosetta 未检查请求是否完成。',
      ),
      noticeCommit: tr(
        'File written. Rosetta did not check whether the requested change was fulfilled.',
        '文件已写入；Rosetta 未检查请求是否完成。',
      ),
      noticeClose: tr('Session released.', '会话已释放。'),
      noticeNoChat: tr(
        'This status strip could not identify the current chat, so actions are disabled. Use the Rosetta tools in the chat instead.',
        '此状态条无法识别当前聊天，操作已禁用。请改用聊天中的 Rosetta 工具。',
      ),
    }

    // Only tokens reported by the live `Theme` Inspect provider are used, so a
    // renamed token degrades the appearance instead of breaking the strip.
    const box = {
      border: '1px solid var(--dsw-alias-border-l1)',
      borderRadius: '10px',
      background: 'var(--dsw-alias-bg-layer-2)',
      color: 'var(--dsw-alias-label-primary)',
      font: 'inherit',
      fontSize: '12px',
      padding: '4px 8px',
    }

    const headerRow = {
      display: 'flex',
      alignItems: 'center',
      gap: '6px',
      flexWrap: 'wrap',
      minWidth: 0,
    }

    const chip = {
      border: '1px solid var(--dsw-alias-border-l1)',
      borderRadius: '999px',
      padding: '1px 7px',
      lineHeight: '18px',
      whiteSpace: 'nowrap',
      color: 'var(--dsw-alias-label-secondary)',
    }

    const pathChip = {
      ...chip,
      maxWidth: '34ch',
      overflow: 'hidden',
      textOverflow: 'ellipsis',
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
      fontSize: '11px',
    }

    const selectStyle = {
      ...chip,
      maxWidth: '40ch',
      padding: '1px 6px',
      background: 'var(--dsw-alias-bg-layer-2)',
      color: 'var(--dsw-alias-label-primary)',
      font: 'inherit',
      fontSize: '12px',
      cursor: 'pointer',
    }

    const chipIdle = { ...chip, color: 'var(--dsw-alias-state-idle-primary)' }

    const button = {
      border: '1px solid var(--dsw-alias-border-l2)',
      borderRadius: '6px',
      background: 'transparent',
      color: 'var(--dsw-alias-label-secondary)',
      cursor: 'pointer',
      font: 'inherit',
      fontSize: '12px',
      lineHeight: '18px',
      padding: '1px 8px',
    }

    const quickButton = {
      ...button,
      borderRadius: '999px',
      padding: '2px 10px',
    }

    const primaryButton = {
      ...quickButton,
      background: 'var(--dsw-alias-bg-layer-1)',
      fontWeight: 600,
      color: 'var(--dsw-alias-label-primary)',
    }

    const miniButton = {
      ...button,
      border: '1px solid transparent',
      padding: '0 5px',
      fontSize: '11px',
      color: 'var(--dsw-alias-label-secondary)',
    }

    function buttonStyle(base, disabled) {
      return disabled ? { ...base, opacity: 0.45, cursor: 'not-allowed' } : base
    }

    const code = {
      margin: 0,
      padding: '6px 8px',
      borderRadius: '6px',
      background: 'var(--dsw-alias-bg-layer-1)',
      color: 'var(--dsw-alias-label-primary)',
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
      fontSize: '11px',
      lineHeight: 1.45,
      maxHeight: '240px',
      maxWidth: '100%',
      overflow: 'auto',
      // `pre` (not `pre-wrap`) keeps long lines intact and lets the block
      // scroll horizontally as well as vertically instead of forcing the whole
      // composer dock wider.
      whiteSpace: 'pre',
    }

    // The expanded details can contain several artifact previews. Bound the
    // panel to a viewport-relative height and scroll inside it, so a long
    // document cannot push the composer off screen.
    const details = {
      display: 'grid',
      gap: '8px',
      maxHeight: 'min(60vh, 520px)',
      overflow: 'auto',
      paddingRight: '4px',
    }

    const noticeStyle = {
      display: 'flex',
      gap: '6px',
      alignItems: 'flex-start',
      border: '1px solid var(--dsw-alias-border-l1)',
      borderLeftWidth: '3px',
      borderRadius: '6px',
      padding: '4px 8px',
      lineHeight: 1.4,
      minWidth: 0,
    }

    const muted = { color: 'var(--dsw-alias-label-secondary)' }
    const faint = { color: 'var(--dsw-alias-state-idle-primary)' }

    function useSnapshot({ includeContent = false, chatSessionId = '', full = false } = {}) {
      const [snapshot, setSnapshot] = React.useState(null)
      const [error, setError] = React.useState(null)
      const [revision, setRevision] = React.useState(0)
      const lastChat = React.useRef('')

      React.useEffect(() => {
        let cancelled = false
        let timer = null

        // A chat switch must not keep the previous chat's strip on screen while
        // the next poll is in flight. Opening the details is not a chat switch,
        // so it must not blank the strip; only a changed chat id resets it.
        if (lastChat.current !== chatSessionId) {
          lastChat.current = chatSessionId
          setSnapshot(null)
          setError(null)
        }

        // The ambient poll asks for metadata only. Artifact bodies are a
        // separate, explicit request (`?content=1`) made when the user expands
        // the details, so an always-on widget never becomes a content feed.
        const query = []
        if (chatSessionId) query.push(`chat=${encodeURIComponent(chatSessionId)}`)
        if (includeContent) query.push('content=1')
        if (full) query.push('full=1')
        const url = query.length ? `${STATE_URL}?${query.join('&')}` : STATE_URL

        const poll = async () => {
          try {
            const response = await fetch(url, { headers: { accept: 'application/json' } })
            if (response.status === 401 || response.status === 403 || response.status === 503) {
              if (!cancelled) {
                setSnapshot({ ok: false, reason: `the Harness refused this request (HTTP ${response.status})` })
                setError(null)
              }
              return
            }
            const body = await response.json()
            if (cancelled) return
            setSnapshot(body)
            setError(null)
          } catch (failure) {
            if (cancelled) return
            setError(String((failure && failure.message) || failure))
          } finally {
            if (!cancelled) timer = setTimeout(poll, POLL_MS)
          }
        }

        poll()
        return () => {
          cancelled = true
          if (timer) clearTimeout(timer)
        }
      }, [includeContent, full, revision, chatSessionId])

      return { snapshot, error, refresh: () => setRevision((value) => value + 1) }
    }

    function shortPath(value) {
      if (typeof value !== 'string' || !value) return ''
      const parts = value.split(/[\\/]/).filter(Boolean)
      return parts.length > 1 ? parts.slice(-2).join('/') : value
    }

    function pairForPath(value) {
      if (typeof value !== 'string') return ''
      const dot = value.lastIndexOf('.')
      if (dot < 0) return ''
      const extension = value.slice(dot).toLowerCase()
      if (extension === '.typ') return 'LaTeX / Typst'
      if (extension === '.sv') return 'Verilog / SystemVerilog'
      if (extension === '.prql') return 'PRQL / SQL'
      if (extension === '.nu') return 'Bash / Nushell (experimental)'
      return ''
    }

    function pairStatusText(pair) {
      const required = pair.available === false
      const optional = Array.isArray(pair.missingOptional) && pair.missingOptional.length > 0
      if (required) {
        const names = Array.isArray(pair.missingRequired) ? pair.missingRequired : []
        return `${ui.pairBlocked}: ${names.join(', ')}`
      }
      if (optional) return `${ui.pairDegraded}: ${pair.missingOptional.join(', ')}`
      return ui.pairReady
    }

    function diffLines(before, after) {
      const rawBefore = String(before == null ? '' : before)
      const rawAfter = String(after == null ? '' : after)
      const countLines = (text) => {
        let count = 0
        for (let index = 0; index < text.length; index += 1) {
          if (text[index] === '\n' || (text[index] === '\r' && text[index + 1] !== '\n')) count += 1
        }
        return count + (text.length > 0 && !/(?:\r\n|\r|\n)$/.test(text) ? 1 : 0)
      }
      if (rawBefore === rawAfter) {
        return { ops: [], additions: 0, deletions: 0, changed: false, large: false, formatNotes: [] }
      }
      const split = (text) => {
        const normalized = text.replace(/\r\n?/g, '\n').replace(/\n$/, '')
        return normalized === '' ? [] : normalized.split('\n')
      }
      const lineEndingKinds = (text) => new Set((text.match(/\r\n|\r|\n/g) || []))
      const sameSet = (left, right) =>
        left.size === right.size && [...left].every((value) => right.has(value))
      const formatNotes = []
      if (!sameSet(lineEndingKinds(rawBefore), lineEndingKinds(rawAfter))) {
        formatNotes.push(ui.diffLineEndings)
      }
      const hasFinalNewline = (text) => /(?:\r\n|\r|\n)$/.test(text)
      if (hasFinalNewline(rawBefore) !== hasFinalNewline(rawAfter)) {
        formatNotes.push(ui.diffFinalNewline)
      }
      if (Math.max(countLines(rawBefore), countLines(rawAfter)) > 100_000) {
        return {
          ops: [], additions: null, deletions: null, changed: true, large: true,
          rowsOmitted: true, longLines: false, lineDetailOmitted: true, formatNotes,
        }
      }
      const a = split(rawBefore)
      const b = split(rawAfter)

      // Common prefix/suffix keep the expensive LCS on the actual change.
      let start = 0
      while (start < a.length && start < b.length && a[start] === b[start]) start += 1
      let endA = a.length - 1
      let endB = b.length - 1
      while (endA >= start && endB >= start && a[endA] === b[endB]) {
        endA -= 1
        endB -= 1
      }
      const midA = a.slice(start, endA + 1)
      const midB = b.slice(start, endB + 1)

      let midOps = []
      let large = false
      if (midA.length * midB.length <= 250_000 && Math.max(midA.length, midB.length) <= 5_000) {
        const rows = midA.length + 1
        const cols = midB.length + 1
        const dp = Array.from({ length: rows }, () => new Uint32Array(cols))
        for (let i = midA.length - 1; i >= 0; i -= 1) {
          for (let j = midB.length - 1; j >= 0; j -= 1) {
            dp[i][j] =
              midA[i] === midB[j]
                ? dp[i + 1][j + 1] + 1
                : Math.max(dp[i + 1][j], dp[i][j + 1])
          }
        }
        let i = 0
        let j = 0
        while (i < midA.length && j < midB.length) {
          if (midA[i] === midB[j]) {
            midOps.push({ kind: ' ', text: midA[i] })
            i += 1
            j += 1
          } else if (dp[i + 1][j] >= dp[i][j + 1]) {
            midOps.push({ kind: '-', text: midA[i] })
            i += 1
          } else {
            midOps.push({ kind: '+', text: midB[j] })
            j += 1
          }
        }
        while (i < midA.length) {
          midOps.push({ kind: '-', text: midA[i] })
          i += 1
        }
        while (j < midB.length) {
          midOps.push({ kind: '+', text: midB[j] })
          j += 1
        }
      } else {
        // Avoid a quadratic LCS and avoid materializing millions of changed
        // lines. Show both ends of the changed block and retain exact block
        // sizes in the summary; the UI labels this as a coarse replacement.
        large = true
        const edge = (items, kind) => {
          const shown = items.slice(0, 2).map((text) => ({ kind, text }))
          if (items.length > 4) shown.push({ kind: '…', text: '' })
          if (items.length > 2) shown.push(...items.slice(-2).map((text) => ({ kind, text })))
          return shown
        }
        midOps = [...edge(midA, '-'), ...edge(midB, '+')]
      }

      const additions = large ? midB.length : midOps.filter((op) => op.kind === '+').length
      const deletions = large ? midA.length : midOps.filter((op) => op.kind === '-').length
      if (additions === 0 && deletions === 0) {
        return { ops: [], additions: 0, deletions: 0, changed: false, large: false, formatNotes }
      }

      const prefixContext = midOps[0]?.kind !== ' '
        ? a.slice(Math.max(0, start - 2), start).map((text) => ({ kind: ' ', text }))
        : []
      const suffixContext = midOps[midOps.length - 1]?.kind !== ' '
        ? a.slice(endA + 1, endA + 3).map((text) => ({ kind: ' ', text }))
        : []
      let trimmed
      let rowsOmitted = large && (midA.length > 4 || midB.length > 4)
      if (large) {
        trimmed = [...prefixContext, ...midOps, ...suffixContext]
      } else {
        // Keep context only inside the changed span. Unchanged prefixes and
        // suffixes may contain millions of lines in a large source file.
        const keep = new Set()
        midOps.forEach((op, index) => {
          if (op.kind === ' ') return
          for (let offset = -2; offset <= 2; offset += 1) {
            const at = index + offset
            if (at >= 0 && at < midOps.length) keep.add(at)
          }
        })
        const middle = []
        let last = -1
        for (let index = 0; index < midOps.length; index += 1) {
          if (!keep.has(index)) continue
          if (last >= 0 && index > last + 1) middle.push({ kind: '…', text: '' })
          middle.push(midOps[index])
          last = index
        }
        trimmed = [...prefixContext, ...middle, ...suffixContext]
      }
      // Bound DOM nodes for files with thousands of changed lines. Preserve the
      // true counts and both ends, and explicitly tell the user lines are hidden.
      rowsOmitted = rowsOmitted || trimmed.length > 500
      const shown = trimmed.length > 500
        ? [...trimmed.slice(0, 250), { kind: '…', text: '' }, ...trimmed.slice(-250)]
        : trimmed
      let longLines = false
      const bounded = shown.map((op) => {
        if (op.text.length <= 2400) return op
        longLines = true
        let left = op.text.slice(0, 1200)
        let right = op.text.slice(-1200)
        // Avoid displaying half of a UTF-16 surrogate pair at either boundary.
        if (/[\uD800-\uDBFF]$/.test(left)) left = left.slice(0, -1)
        if (/^[\uDC00-\uDFFF]/.test(right)) right = right.slice(1)
        return { ...op, text: left + ' … ' + right }
      })
      return { ops: bounded, additions, deletions, changed: true, large, rowsOmitted, longLines, formatNotes }
    }

    function pairSummary(pairs) {
      const list = Array.isArray(pairs) ? pairs : []
      const ready = list.filter((pair) => pair.available !== false)
      const blocked = list.filter((pair) => pair.available === false)
      const degraded = ready.filter(
        (pair) => Array.isArray(pair.missingOptional) && pair.missingOptional.length > 0,
      )
      return {
        list,
        ready: ready.length,
        total: list.length,
        blocked,
        degraded,
        issues: blocked.length + degraded.length,
      }
    }

    function hasConvertibleContent(entry) {
      return entry.hasConvertibleContent !== false
    }

    function previewLabel(entry) {
      if (!entry.previewCandidate && !entry.hasPreview) return null
      if (!hasConvertibleContent(entry)) return ui.statusCandidateNoConvertible
      if (entry.viewEdited === false) return ui.statusCandidateUnchanged
      return ui.statusCandidateUnverified
    }

    function viewLabel(entry) {
      if (!hasConvertibleContent(entry)) {
        return { tone: 'warn', text: ui.statusPassThrough }
      }
      if (entry.viewEdited === true) {
        return { tone: 'warn', text: ui.statusEdited }
      }
      return { tone: 'good', text: ui.statusSynced }
    }

    function deliverDisabledReason(entry, canAct, busy) {
      if (busy) return ui.working
      if (!canAct) return ui.scopeUnknown
      if (!hasConvertibleContent(entry)) return ui.deliverNoConvertible
      if (entry.viewEdited !== true) return ui.deliverNoEdit
      if (!(entry.previewCandidate || entry.hasPreview)) return ui.deliverFirst
      if ((entry.targetDrift || {}).drifted) return ui.deliverDrift
      return ''
    }

    function previewDisabledReason(canAct, busy) {
      if (busy) return ui.working
      if (!canAct) return ui.scopeUnknown
      return ''
    }

    function ownerLabel(entry, chatSessionId) {
      if (!entry.ownerSessionId) return ui.ownerUnknown
      if (chatSessionId && entry.ownerSessionId === chatSessionId) return ui.ownerThis
      return entry.ownerSessionId
    }

    function copyText(value) {
      try {
        if (
          typeof navigator !== 'undefined' &&
          navigator.clipboard &&
          typeof navigator.clipboard.writeText === 'function'
        ) {
          const pending = navigator.clipboard.writeText(value)
          if (pending && typeof pending.catch === 'function') pending.catch(() => {})
        }
      } catch {
        // Clipboard access is a convenience; never let it break the strip.
      }
    }

    function CopyButton({ value, label }) {
      return h(
        'button',
        {
          type: 'button',
          style: miniButton,
          title: label,
          'aria-label': label,
          onClick: (event) => {
            event.stopPropagation()
            copyText(value)
          },
        },
        ui.copy,
      )
    }

    function Row({ label, value, copyValue, title }) {
      return h(
        'div',
        { style: { display: 'flex', gap: '6px', alignItems: 'baseline', minWidth: 0 } },
        h('span', { style: { ...faint, flex: '0 0 112px', minWidth: 0 } }, label),
        h(
          'span',
          { style: { wordBreak: 'break-word', flex: 1, minWidth: 0 }, title: title || undefined },
          value,
        ),
        copyValue
          ? h(CopyButton, { value: copyValue, label: `${ui.copy}: ${label}` })
          : null,
      )
    }

    function Section({ title, children }) {
      const items = (Array.isArray(children) ? children : [children]).filter(Boolean)
      if (items.length === 0) return null
      return h(
        'section',
        { style: { display: 'grid', gap: '3px', minWidth: 0 } },
        h(
          'div',
          {
            style: {
              ...faint,
              fontSize: '10px',
              fontWeight: 600,
              letterSpacing: '.06em',
              textTransform: 'uppercase',
            },
          },
          title,
        ),
        ...items,
      )
    }

    function DiffBlock({ before, after, truncated = false, title = ui.diff }) {
      if (typeof after !== 'string' || after.length === 0) return null
      const diff = diffLines(before || '', after)
      if (!diff.changed) {
        return h(
          Section,
          { title },
          h(Row, { label: ui.diff, value: truncated ? ui.diffEmptyTruncated : ui.diffEmpty }),
          ...diff.formatNotes.map((note, index) => h('div', { key: `format-${index}`, style: { ...faint, paddingBottom: '2px' } }, note)),
          truncated ? h('div', { style: { ...faint, paddingBottom: '2px' } }, ui.diffTruncated) : null,
        )
      }
      return h(
        Section,
        { title },
        diff.lineDetailOmitted
          ? h('div', { style: { ...faint, paddingBottom: '2px' } }, ui.diffTooManyLines)
          : h('div', { style: { ...faint, paddingBottom: '2px' } }, ui.diffSummary(diff.additions, diff.deletions)),
        ...diff.formatNotes.map((note, index) => h('div', { key: `format-${index}`, style: { ...faint, paddingBottom: '2px' } }, note)),
        diff.large ? h('div', { style: { ...faint, paddingBottom: '2px' } }, ui.diffLarge) : null,
        diff.rowsOmitted ? h('div', { style: { ...faint, paddingBottom: '2px' } }, ui.diffRowsOmitted) : null,
        diff.longLines ? h('div', { style: { ...faint, paddingBottom: '2px' } }, ui.diffLongLines) : null,
        truncated ? h('div', { style: { ...faint, paddingBottom: '2px' } }, ui.diffTruncated) : null,
        h(
          'div',
          {
            style: {
              ...code,
              maxHeight: '260px',
              overflow: 'auto',
              whiteSpace: 'pre',
            },
          },
          ...diff.ops.map((op, index) =>
            h(
              'div',
              {
                key: index,
                style: {
                  color:
                    op.kind === '+'
                      ? 'var(--dsw-alias-state-success-primary)'
                      : op.kind === '-'
                        ? 'var(--dsw-alias-state-error-primary)'
                        : 'var(--dsw-alias-label-secondary)',
                },
              },
              `${op.kind} ${op.text}`,
            ),
          ),
        ),
      )
    }

    function PairStatus({ pairs }) {
      if (!Array.isArray(pairs) || pairs.length === 0) return null
      return h(
        Section,
        { title: ui.pairs },
        ...pairs.map((pair) =>
          h(Row, {
            key: pair.name,
            label: pair.name,
            value: pairStatusText(pair),
            title: pair.hint || undefined,
          }),
        ),
      )
    }

    function PreviewBlock({ title, content, expanded = false }) {
      if (!content) return null
      const heading = h(
        'div',
        {
          style: {
            color: 'var(--dsw-alias-label-secondary)',
            padding: '2px 0',
            cursor: expanded ? 'default' : 'pointer',
          },
        },
        title,
      )
      return h(
        'div',
        { style: { display: 'flex', gap: '6px', alignItems: 'flex-start', minWidth: 0 } },
        expanded
          ? h(
              'div',
              { style: { flex: 1, minWidth: 0 } },
              heading,
              h('pre', { style: code }, content),
            )
          : h(
              'details',
              { style: { flex: 1, minWidth: 0, paddingTop: '2px' } },
              h('summary', { style: { cursor: 'pointer', color: 'var(--dsw-alias-label-secondary)' } }, title),
              h('pre', { style: code }, content),
            ),
        h(CopyButton, { value: content, label: `${ui.copy}: ${title}` }),
      )
    }

    function StatusPill({ tone, text, title }) {
      const color =
        tone === 'good'
          ? 'var(--dsw-alias-state-success-primary)'
          : tone === 'error'
            ? 'var(--dsw-alias-state-error-primary)'
            : tone === 'idle'
              ? 'var(--dsw-alias-state-idle-primary)'
              : 'var(--dsw-alias-state-warn-primary)'
      return h(
        'span',
        { style: { ...chip, display: 'inline-flex', alignItems: 'center', gap: '4px' }, title },
        h(
          'span',
          {
            style: {
              width: '6px',
              height: '6px',
              borderRadius: '999px',
              background: color,
              display: 'inline-block',
              flex: 'none',
            },
          },
        ),
        text,
      )
    }

    function SessionDetail({
      entry,
      onAction,
      busy,
      canAct,
      scopeNote,
      chatSessionId,
      canOpenInChat = false,
      onOpenInChat = null,
      canReleaseOther = false,
      onReleaseOther = null,
      fullView = false,
      onLoadFull = null,
      hostGeneration = null,
    }) {
      const drift = entry.targetDrift || {}
      const meta = entry.projectionMetadata || {}
      const convertible = hasConvertibleContent(entry)
      const viewEdited = entry.viewEdited === true
      const candidate = Boolean(entry.previewCandidate || entry.hasPreview)
      const deliverWhy = deliverDisabledReason(entry, canAct, busy)
      const actionNote = !canAct
        ? canReleaseOther
          ? ui.otherManageHint
          : canOpenInChat
            ? ui.otherOpenHint
            : scopeNote || ui.scopeUnknown
        : deliverWhy
      const previewText = !candidate
        ? ui.previewNone
        : convertible
          ? ui.previewCandidate
          : ui.previewCandidateNoConvertible
      const actionSection = h(
        Section,
        { title: ui.actions },
        h(
          'div',
          { style: { display: 'flex', gap: '6px', flexWrap: 'wrap', alignItems: 'center' } },
          !canAct && canOpenInChat
            ? h(
                'button',
                {
                  type: 'button',
                  style: buttonStyle(primaryButton, busy),
                  disabled: busy,
                  title: ui.openHint,
                  onClick: () => onOpenInChat && onOpenInChat(entry),
                },
                ui.openInThisChat,
              )
            : null,
          !canAct && canReleaseOther
            ? h(
                'button',
                {
                  type: 'button',
                  style: buttonStyle(
                    { ...button, color: 'var(--dsw-alias-state-error-primary)' },
                    busy,
                  ),
                  disabled: busy,
                  title: ui.releaseOther,
                  onClick: () => onReleaseOther && onReleaseOther(entry),
                },
                ui.releaseOther,
              )
            : null,
          h(
            'button',
            {
              type: 'button',
              style: buttonStyle(quickButton, busy || !canAct),
              disabled: busy || !canAct,
              title: previewDisabledReason(canAct, busy) || ui.previewHint,
              onClick: () => onAction('preview', entry),
            },
            ui.preview,
          ),
          canAct && entry.hasPreview === true
            ? h(
                'button',
                {
                  type: 'button',
                  style: buttonStyle(button, busy),
                  disabled: busy,
                  title: ui.discardPreviewHint,
                  onClick: () => onAction('discard-preview', entry),
                },
                ui.discardPreview,
              )
            : null,
          h(
            'button',
            {
              type: 'button',
              style: buttonStyle(primaryButton, Boolean(deliverWhy)),
              disabled: Boolean(deliverWhy),
              title: deliverWhy || ui.deliverHint,
              onClick: () => onAction('commit', entry),
            },
            ui.deliver,
          ),
          canAct && entry.restoreAvailable === true
            ? h(
                'button',
                {
                  type: 'button',
                  style: buttonStyle(button, busy),
                  disabled: busy,
                  title: ui.restoreHint,
                  onClick: () => onAction('restore-last', entry),
                },
                ui.restoreLast,
              )
            : null,
          h(
            'button',
            {
              type: 'button',
              style: buttonStyle(button, busy || !canAct),
              disabled: busy || !canAct,
              title: canAct ? ui.release : ui.scopeUnknown,
              onClick: () => onAction('close', entry),
            },
            ui.release,
          ),
          actionNote
            ? h('span', { style: { ...faint, flexBasis: '100%' } }, actionNote)
            : null,
        ),
      )
      return h(
        'div',
        { style: { display: 'grid', gap: '8px', paddingTop: '4px', minWidth: 0 } },
        h(
          Section,
          { title: ui.artifact },
          h(Row, { label: ui.target, value: entry.targetPath, copyValue: entry.targetPath }),
          h(Row, { label: ui.pair, value: entry.pair }),
          h(Row, { label: ui.session, value: entry.session }),
          h(Row, { label: ui.owner, value: ownerLabel(entry, chatSessionId) }),
          hostGeneration
            ? h(Row, {
                label: ui.build,
                value: String(hostGeneration).slice(0, 12),
                title: String(hostGeneration),
              })
            : null,
          h('div', { style: { ...faint, lineHeight: 1.4 } }, ui.fenceScope),
          entry.active === false
            ? h(Row, {
                label: ui.protection,
                value: entry.dirty || entry.previewCandidate ? ui.idleDraft : ui.released,
              })
            : null,
          entry.active === false && (entry.dirty || entry.previewCandidate)
            ? h('div', { style: { ...faint, lineHeight: 1.4 } }, ui.draftRetention)
            : null,
        ),
        h(
          Section,
          { title: ui.delivery },
          h(Row, {
            label: ui.convertible,
            value: convertible ? ui.convertiblePresent : ui.convertibleNone,
          }),
          h(Row, {
            label: ui.familiar,
            value: entry.dirty ? ui.familiarDirty : ui.familiarClean,
          }),
          h(Row, {
            label: ui.viewEdit,
            value: viewEdited ? ui.viewEdited : ui.viewUnchanged,
          }),
          h(Row, {
            label: ui.targetDisk,
            value: drift.drifted ? ui.targetChanged(drift.reason) : ui.targetUnchanged,
          }),
          h(Row, { label: ui.previewPending, value: previewText }),
          meta.method || meta.engine
            ? h(Row, { label: ui.backend, value: String(meta.method || meta.engine) })
            : null,
          meta.preservedRegions && meta.preservedRegions.length
            ? h(Row, {
                label: ui.statusPassThrough,
                value: ui.passthrough(
                  meta.preservedRegions.length,
                  meta.preservedRegions.join('; '),
                ),
              })
            : null,
        ),
        actionSection,
        h(
          'div',
          { style: { ...faint, display: 'flex', gap: '6px', alignItems: 'center' } },
          fullView
            ? h('span', {}, ui.fullViewLoaded)
            : onLoadFull
              ? h(
                  'button',
                  {
                    type: 'button',
                    style: buttonStyle(button, busy),
                    disabled: busy,
                    title: ui.loadFullViewHint,
                    onClick: () => onLoadFull(),
                  },
                  ui.loadFullView,
                )
              : null,
        ),
        h(PreviewBlock, { title: ui.targetView, content: entry.targetPreview }),
        h(PreviewBlock, { title: ui.agentView, content: entry.agentViewPreview }),
        h(DiffBlock, {
          before: entry.targetPreview || '',
          after: entry.preview || '',
          truncated:
            !fullView
            && (
              (entry.targetPreview || '').length >= 4000
              || (entry.preview || '').length >= 4000
            ),
        }),
        h(PreviewBlock, {
          title: ui.candidateDelivery,
          content: entry.preview,
          // The candidate is the thing the user just asked to see; show it
          // immediately once the details panel opens (the panel itself scrolls).
          expanded: true,
        }),
      )
    }

    function RosettaStatus({ sessionId } = {}) {
      const [open, setOpen] = React.useState(false)
      const [busy, setBusy] = React.useState(false)
      const [actionNotice, setActionNotice] = React.useState(null)
      // The other-chat sessions are loaded only when the user clicks the
      // "+N other" chip; the ambient poll stays scoped to this chat.
      const [others, setOthers] = React.useState({
        open: false,
        sessions: null,
        error: null,
      })
      // The user's explicitly chosen primary session. Empty means "first
      // active"; the value is only a preference, so a closed session falls
      // back safely instead of forcing a stale target.
      const [selectedSession, setSelectedSession] = React.useState('')
      const [pendingConfirm, setPendingConfirm] = React.useState(null)
      const [fullView, setFullView] = React.useState(false)
      const fileInputRef = React.useRef(null)
      // The slot is session-scoped, so its standard props carry the current
      // chat's stable Session id. An empty value is a real fallback, not a
      // reason to guess: without it the strip cannot tell whose preview it sees.
      const chatSessionId =
        typeof sessionId === 'string' && sessionId.trim() ? sessionId.trim() : ''
      // Metadata always; artifact bodies only while the details are expanded.
      const { snapshot, error, refresh } = useSnapshot({
        includeContent: open,
        chatSessionId,
        full: fullView,
      })

      React.useEffect(() => {
        setActionNotice(null)
        setOthers({ open: false, sessions: null, error: null })
        setSelectedSession('')
        setPendingConfirm(null)
        setFullView(false)
      }, [chatSessionId])

      const loadOthers = async () => {
        if (others.open) {
          setOthers({ open: false, sessions: others.sessions, error: null })
          return
        }
        setOthers({ open: true, sessions: null, error: null })
        try {
          const response = await fetch(STATE_URL, {
            headers: { accept: 'application/json' },
          })
          if (response.status === 401 || response.status === 403 || response.status === 503) {
            throw new Error(`the Harness refused this request (HTTP ${response.status})`)
          }
          const body = await response.json()
          const all = body.sessions || []
          setOthers({
            open: true,
            sessions: all.filter((entry) => entry.ownerSessionId !== chatSessionId),
            error: null,
          })
        } catch (failure) {
          setOthers({
            open: true,
            sessions: null,
            error: String((failure && failure.message) || failure),
          })
        }
      }

      const performAction = async (action, entry, expected = null) => {
        if (busy) return
        if (!chatSessionId) {
          setActionNotice({ tone: 'error', text: ui.noticeNoChat })
          return
        }
        setBusy(true)
        setActionNotice(null)
        try {
          const requestBody =
            action === 'open'
              ? {
                  action,
                  targetPath: entry.targetPath,
                  pair: entry.pair,
                  harnessSessionId: chatSessionId,
                }
              : action === 'release-other'
                ? {
                    action,
                    session: entry.session,
                    harnessSessionId: chatSessionId,
                    confirmDiscard: true,
                  }
                : action === 'commit'
                  ? {
                      action,
                      session: entry.session,
                      harnessSessionId: chatSessionId,
                      // Bind the write to the exact preview/view/target the
                      // user confirmed. The Host forwards these digests to the
                      // bridge; any later change makes the commit stale.
                      expectedPreviewSha256: expected?.previewSha256 ?? entry.previewSha256 ?? null,
                      expectedViewSha256: expected?.viewSha256 ?? entry.viewSha256 ?? null,
                      expectedTargetSha256: expected?.targetSha256 ?? entry.targetSha256 ?? null,
                    }
                  : {
                      action,
                      session: entry.session,
                      harnessSessionId: chatSessionId,
                    }
          const response = await fetch(ACTION_URL, {
            method: 'POST',
            headers: { accept: 'application/json', 'content-type': 'application/json' },
            // The Host checks this against the owner recorded when the session
            // was opened; it is not trusted as an authorisation by itself.
            body: JSON.stringify(requestBody),
          })
          if (response.status === 401 || response.status === 403) {
            throw new Error('Harness rejected this action. Reopen the conversation and try again.')
          }
          const result = await response.json()
          if (!response.ok || !result.ok) throw new Error(result.error || `HTTP ${response.status}`)
          setActionNotice({
            // A write is a real action, but it is still not proof that the
            // requested change was made. Keep it amber rather than green.
            tone: action === 'open' ? 'good' : 'warn',
            text:
              action === 'open'
                ? ui.noticeOpen
                : action === 'preview'
                  ? ui.noticePreview
                  : action === 'commit'
                    ? ui.noticeCommit
                    : action === 'release-other'
                      ? ui.noticeReleaseOther
                      : action === 'restore-last'
                        ? ui.noticeRestore
                        : action === 'discard-preview'
                          ? ui.noticeDiscardPreview
                          : ui.noticeClose,
          })
          if (action === 'open') {
            setOthers({ open: false, sessions: null, error: null })
          }
          if (action === 'release-other') {
            setOthers((current) => ({
              ...current,
              open: true,
              sessions: (current.sessions || []).filter(
                (candidate) => candidate.session !== entry.session,
              ),
            }))
          }
          // Preview, commit and open all produce something the user should see
          // in the details panel; open it so they do not have to hunt for it.
          if (action === 'restore-last') {
            setOpen(false)
          } else if (
            action === 'preview'
            || action === 'commit'
            || action === 'open'
            || action === 'discard-preview'
          ) {
            setOpen(true)
          }
          refresh()
        } catch (failure) {
          setActionNotice({
            tone: 'error',
            text: String((failure && failure.message) || failure),
          })
        } finally {
          setBusy(false)
        }
      }

      // Deliver and destructive release never write or discard on the first
      // click. Load the current candidate body before arming delivery so the
      // confirmation always contains the exact target diff bound to its hashes.
      const armCommitConfirmation = async (entry) => {
        const expected = {
          previewSha256: entry.previewSha256 ?? null,
          viewSha256: entry.viewSha256 ?? null,
          targetSha256: entry.targetSha256 ?? null,
        }
        setBusy(true)
        setPendingConfirm(null)
        setActionNotice(null)
        setOpen(true)
        try {
          const query = new URLSearchParams({
            chat: chatSessionId,
            content: '1',
            full: '1',
          })
          const response = await fetch(`${STATE_URL}?${query.toString()}`, {
            headers: { accept: 'application/json' },
          })
          if (!response.ok) throw new Error(`HTTP ${response.status}`)
          const body = await response.json()
          const current = (body.sessions || []).find(
            (candidate) => candidate.session === entry.session,
          )
          if (
            body.ok !== true || body.chatScoped !== true || body.contentIncluded !== true ||
            !current || current.hasPreview !== true || typeof current.preview !== 'string' ||
            typeof current.targetPreview !== 'string'
          ) {
            throw new Error('The current preview content is unavailable.')
          }
          const unchanged = Object.entries(expected).every(
            ([key, value]) => (current[key] ?? null) === value,
          )
          if (!unchanged) {
            setActionNotice({ tone: 'warn', text: ui.confirmDiffStale })
            refresh()
            return
          }
          setPendingConfirm({
            action: 'commit',
            entry: { ...entry, ...current },
            expected,
          })
        } catch {
          setActionNotice({ tone: 'error', text: ui.confirmDiffUnavailable })
        } finally {
          setBusy(false)
        }
      }

      // Preview/open stay immediate. Other destructive actions remain inline.
      const onAction = (action, entry) => {
        if (busy) return
        if (!chatSessionId) {
          setActionNotice({ tone: 'error', text: ui.noticeNoChat })
          return
        }
        if (action === 'commit') {
          void armCommitConfirmation(entry)
          return
        }
        if (
          action === 'release-other' ||
          action === 'restore-last' ||
          (action === 'close' && entry.dirty === true)
        ) {
          setPendingConfirm({
            action,
            entry,
            expected: null,
          })
          return
        }
        setPendingConfirm(null)
        void performAction(action, entry)
      }

      const onPickFile = (event) => {
        const input = event.target
        const file = input && input.files && input.files[0]
        if (input) input.value = ''
        if (!file) return
        if (!chatSessionId) {
          setActionNotice({ tone: 'error', text: ui.openFileNoChat })
          return
        }
        const bridge =
          typeof globalThis !== 'undefined' ? globalThis.__DSH_HOST_PATHS__ : undefined
        let targetPath = ''
        try {
          targetPath =
            bridge && typeof bridge.pathFor === 'function'
              ? bridge.pathFor(file)
              : file.path || ''
        } catch {
          targetPath = ''
        }
        if (!targetPath) {
          setActionNotice({ tone: 'error', text: ui.openFileNoBridge })
          return
        }
        const pair = pairForPath(targetPath)
        if (!pair) {
          setActionNotice({ tone: 'error', text: ui.openFileUnsupported })
          return
        }
        void onAction('open', { targetPath, pair, session: '' })
      }

      const canOpenFile = Boolean(chatSessionId) && !busy
      const openFileButton = h(
        'button',
        {
          type: 'button',
          style: buttonStyle(quickButton, !canOpenFile),
          disabled: !canOpenFile,
          title: chatSessionId ? ui.openFileHint : ui.openFileNoChat,
          onClick: () => fileInputRef.current && fileInputRef.current.click(),
        },
        ui.openFile,
      )
      const openFileIconButton = h(
        'button',
        {
          type: 'button',
          style: buttonStyle({ ...button, padding: '1px 6px' }, !canOpenFile),
          disabled: !canOpenFile,
          title: chatSessionId ? ui.openFileHint : ui.openFileNoChat,
          onClick: () => fileInputRef.current && fileInputRef.current.click(),
        },
        '＋',
      )
      const fileInputElement = h('input', {
        ref: fileInputRef,
        type: 'file',
        accept: '.typ,.sv,.prql,.nu',
        style: { display: 'none' },
        onChange: onPickFile,
      })

      const detailList = (
        entries,
        canAct,
        scopeNote,
        canOpenInChat = false,
        canReleaseOther = false,
        fullView = false,
        onLoadFull = null,
      ) =>
        h(
          'div',
          { style: details },
          ...entries.map((entry) =>
            h(SessionDetail, {
              key: entry.session,
              entry,
              onAction,
              busy,
              canAct,
              scopeNote,
              chatSessionId,
              canOpenInChat,
              onOpenInChat: (target) => onAction('open', target),
              canReleaseOther,
              onReleaseOther: (target) => onAction('release-other', target),
              fullView,
              onLoadFull,
              hostGeneration: snapshot?.generation?.digest || null,
            }),
          ),
        )

      const noticeBlock = () => {
        if (!actionNotice) return null
        const tone =
          actionNotice.tone === 'error'
            ? 'var(--dsw-alias-state-error-primary)'
            : actionNotice.tone === 'good'
              ? 'var(--dsw-alias-state-success-primary)'
              : 'var(--dsw-alias-state-warn-primary)'
        return h(
          'div',
          {
            role: 'status',
            'aria-live': actionNotice.tone === 'error' ? 'assertive' : 'polite',
            style: {
              ...noticeStyle,
              borderLeftColor: tone,
              color:
                actionNotice.tone === 'error'
                  ? 'var(--dsw-alias-state-error-primary)'
                  : 'var(--dsw-alias-label-secondary)',
            },
          },
          h('span', { style: { flex: 1, minWidth: 0 } }, actionNotice.text),
          h(
            'button',
            {
              type: 'button',
              style: miniButton,
              title: ui.dismiss,
              'aria-label': ui.dismiss,
              onClick: () => setActionNotice(null),
            },
            '×',
          ),
        )
      }

      const detailsButton = h(
        'button',
        {
          type: 'button',
          style: { ...button, marginLeft: 'auto' },
          onClick: () => setOpen((value) => !value),
          title: open ? ui.hideDetails : ui.details,
          'aria-expanded': open,
        },
        open ? ui.hideDetails : ui.details,
      )

      const refreshButton = h(
        'button',
        {
          type: 'button',
          style: { ...button, padding: '1px 6px' },
          title: ui.refresh,
          'aria-label': ui.refresh,
          onClick: () => {
            if (others.open) loadOthers()
            refresh()
          },
        },
        '↻',
      )

      if (error && !snapshot) {
        return h(
          'div',
          { style: { ...box, ...muted } },
          `${ui.brand}: status unavailable (${error})`,
        )
      }
      if (!snapshot) return null

      if (snapshot.disabled) {
        const workspaceNeeded = String(snapshot.reason || '').startsWith('No `workspaceRoots` configured')
        return h(
          'div',
          { style: { ...box, ...muted } },
          `${ui.brand}: ${workspaceNeeded ? ui.workspaceNeeded : `${tr('not active', '未启用')} - ${snapshot.reason}`}`,
        )
      }
      if (snapshot.ok === false) {
        return h(
          'div',
          { style: { ...box, ...muted } },
          `${ui.brand}: ${snapshot.reason || 'status unavailable'}`,
        )
      }

      // A blank area is indistinguishable from "the plugin did nothing", so the
      // strip always says something. When the Host cannot name the current chat
      // it says exactly that, and never presents a workspace session as this
      // chat's work.
      const all = snapshot.sessions || []
      const chatScoped = snapshot.chatScoped === true
      const mine = chatScoped ? all : []
      const workspaceOthers = chatScoped ? [] : all
      const otherSessionCount = Number(snapshot.otherSessionCount) || 0
      const selected = mine.some((entry) => entry.session === selectedSession)
        ? selectedSession
        : ''
      const active =
        (selected && mine.find((entry) => entry.session === selected)) ||
        mine.find((entry) => entry.active !== false) ||
        null
      const pairInfo = pairSummary(snapshot.pairs)
      const pairTitle = pairInfo.list
        .map((pair) => `${pair.name}: ${pairStatusText(pair)}`)
        .join('\n')
      const pairNeedsAttention =
        pairInfo.issues > 0 || Boolean(snapshot.pairDiagnosticsError)
      const pairChip =
        pairInfo.total === 0
          ? snapshot.pairDiagnosticsError
            ? h(StatusPill, {
                tone: 'error',
                text: ui.pairsUnavailable,
                title: snapshot.pairDiagnosticsError,
              })
            : null
          : pairInfo.issues > 0
            ? h(StatusPill, {
                tone: pairInfo.blocked.length > 0 ? 'error' : 'warn',
                text: ui.pairsChip(pairInfo.ready, pairInfo.total),
                title: pairTitle,
              })
            : null
      const sessionSelect =
        mine.length > 1
          ? h(
              'select',
              {
                value: active ? active.session : '',
                style: selectStyle,
                title: ui.switchSessionHint,
                'aria-label': ui.switchSession,
                onChange: (event) => {
                  setSelectedSession(event.target.value)
                  setPendingConfirm(null)
                },
              },
              ...mine.map((entry) =>
                h(
                  'option',
                  { key: entry.session, value: entry.session },
                  `${entry.pair} · ${shortPath(entry.targetPath)}`,
                ),
              ),
            )
          : null
      const activity = snapshot.activity
      const activityLabel = activity
        ? ui.activity[activity.command] || ui.activity.default
        : ''
      const activityChip =
        !busy && activity
          ? h(StatusPill, {
              tone: 'warn',
              text: ui.agentWorking(activityLabel),
              title: activity.startedAt
                ? new Date(activity.startedAt).toLocaleTimeString()
                : undefined,
            })
          : null
      const pending = pendingConfirm
      const pendingDiff =
        pending && pending.action === 'commit' && pending.entry.preview
          ? diffLines(pending.entry.targetPreview || '', pending.entry.preview)
          : null
      const confirmPanel = pending
        ? (() => {
            const title =
              pending.action === 'commit'
                ? ui.confirmDeliverTitle
                : pending.action === 'release-other'
                  ? ui.confirmReleaseOtherTitle
                  : pending.action === 'restore-last'
                    ? ui.confirmRestoreTitle
                    : ui.confirmReleaseDirtyTitle
            const hint =
              pending.action === 'commit'
                ? pending.entry.active === false
                  ? `${ui.confirmDeliverHint} ${ui.idleDeliverNote}`
                  : ui.confirmDeliverHint
                : pending.action === 'release-other'
                  ? ui.confirmReleaseOther(pending.entry.session, pending.entry.targetPath)
                  : pending.action === 'restore-last'
                    ? ui.confirmRestoreHint
                    : ui.confirmReleaseDirtyHint
            const label =
              pending.action === 'commit'
                ? ui.confirmDelivery
                : pending.action === 'restore-last'
                  ? ui.confirmRestoreAction
                  : ui.confirmReleaseAction
            return h(
              'div',
              {
                role: 'alert',
                style: {
                  display: 'grid',
                  gap: '6px',
                  padding: '8px',
                  border: '1px solid var(--dsw-alias-state-warn-primary)',
                  borderRadius: '8px',
                  background: 'var(--dsw-alias-bg-layer-2)',
                },
              },
              h('div', { style: { fontWeight: 600 } }, title),
              h('div', { style: faint }, hint),
              pendingDiff
                ? h(
                    'div',
                    { style: { display: 'grid', gap: '4px' } },
                    h(DiffBlock, {
                      before: pending.entry.targetPreview || '',
                      after: pending.entry.preview,
                      title: ui.confirmTargetDiff,
                      // Confirmation is armed only after the full content=1
                      // response is fetched and its digests are checked.
                      truncated: false,
                    }),
                    pendingDiff.lineDetailOmitted
                      ? h(PreviewBlock, {
                          title: ui.targetView,
                          content: pending.entry.targetPreview,
                        })
                      : null,
                    pendingDiff.lineDetailOmitted
                      ? h(PreviewBlock, {
                          title: ui.deliveryText,
                          content: pending.entry.preview,
                        })
                      : null,
                    h('div', { style: faint }, ui.confirmTargetDiffNote),
                  )
                : null,
              h(
                'div',
                {
                  style: {
                    ...pathChip,
                    maxWidth: '100%',
                    background: 'transparent',
                    padding: 0,
                  },
                },
                pending.entry.targetPath,
              ),
              h(
                'div',
                { style: { display: 'flex', gap: '6px', flexWrap: 'wrap' } },
                h(
                  'button',
                  {
                    type: 'button',
                    style: buttonStyle(primaryButton, busy),
                    disabled: busy,
                    onClick: () => {
                      const confirmation = pending
                      setPendingConfirm(null)
                      void performAction(
                        confirmation.action,
                        confirmation.entry,
                        confirmation.expected || null,
                      )
                    },
                  },
                  label,
                ),
                h(
                  'button',
                  {
                    type: 'button',
                    style: buttonStyle(button, busy),
                    disabled: busy,
                    onClick: () => setPendingConfirm(null),
                  },
                  ui.cancel,
                ),
              ),
            )
          })()
        : null
      const pairPanel = () => {
        if (!open) return null
        if (snapshot.pairDiagnosticsError && pairInfo.total === 0) {
          return h(
            'div',
            {
              style: {
                ...noticeStyle,
                borderLeftColor: 'var(--dsw-alias-state-error-primary)',
                color: 'var(--dsw-alias-state-error-primary)',
              },
            },
            snapshot.pairDiagnosticsError,
          )
        }
        return h(PairStatus, { pairs: snapshot.pairs })
      }

      const otherChipButton =
        otherSessionCount > 0
          ? h(
              'button',
              {
                type: 'button',
                style: {
                  ...chip,
                  background: 'transparent',
                  font: 'inherit',
                  fontSize: '12px',
                  cursor: 'pointer',
                  color: others.open
                    ? 'var(--dsw-alias-label-primary)'
                    : 'var(--dsw-alias-label-secondary)',
                },
                title: ui.otherSessionsHint,
                'aria-expanded': others.open,
                onClick: loadOthers,
              },
              ui.otherChip(otherSessionCount),
            )
          : null

      const renderOthersPanel = () => {
        if (!others.open) return null
        return h(
          'div',
          {
            style: {
              borderTop: '1px solid var(--dsw-alias-border-l1)',
              paddingTop: '6px',
              display: 'grid',
              gap: '6px',
            },
          },
          h(
            'div',
            { style: headerRow },
            h('span', { style: { fontWeight: 600 } }, ui.otherSessions),
            h(
              'span',
              { style: chipIdle },
              String(others.sessions ? others.sessions.length : otherSessionCount),
            ),
            h(
              'button',
              {
                type: 'button',
                style: { ...button, marginLeft: 'auto' },
                onClick: loadOthers,
              },
              ui.hide,
            ),
          ),
          others.error
            ? h(
                'div',
                {
                  style: {
                    ...noticeStyle,
                    borderLeftColor: 'var(--dsw-alias-state-error-primary)',
                    color: 'var(--dsw-alias-state-error-primary)',
                  },
                },
                others.error,
              )
            : others.sessions === null
              ? h('span', { style: faint }, ui.loading)
              : others.sessions.length
                ? detailList(
                    others.sessions,
                    false,
                    ui.scopeOther,
                    Boolean(chatSessionId),
                    Boolean(chatSessionId),
                  )
                : h('span', { style: faint }, ui.otherSessionsEmpty),
        )
      }

      if (!active) {
        if (!chatScoped && workspaceOthers.length) {
          return h(
            'div',
            { style: { ...box, display: 'grid', gap: '6px' } },
            h(
              'div',
              { style: headerRow },
              h('span', { style: { fontWeight: 700 } }, ui.brand),
              h('span', {}, ui.otherTitle),
              h('span', { style: chipIdle }, ui.idle),
              pairChip,
              activityChip,
              detailsButton,
            ),
            confirmPanel,
            h('span', { style: { ...muted, lineHeight: 1.4 } }, ui.otherNotice),
            open ? detailList(workspaceOthers, false, ui.scopeOther) : null,
            pairPanel(),
            noticeBlock(),
          )
        }

        const finished = mine.length
        const hidden = chatScoped && otherSessionCount > 0 ? ui.hidden(otherSessionCount) : null
        return h(
          'div',
          { style: { ...box, display: 'grid', gap: '4px' } },
          h(
            'div',
            { style: headerRow },
            h('span', { style: { fontWeight: 700 } }, ui.brand),
            h('span', { style: chipIdle }, ui.idle),
            h(
              'span',
              { style: muted },
              finished ? ui.finished(finished) : chatScoped ? ui.noArtifact : ui.ready,
            ),
            h(
              'div',
              {
                style: {
                  display: 'flex',
                  gap: '4px',
                  alignItems: 'center',
                  marginLeft: 'auto',
                },
              },
              openFileButton,
              pairChip,
              activityChip,
              finished || pairNeedsAttention ? detailsButton : null,
            ),
          ),
          confirmPanel,
          hidden
            ? h(
                'div',
                { style: headerRow },
                h('span', { style: faint }, hidden),
                otherChipButton,
              )
            : null,
          chatScoped && !finished
            ? h('span', { style: { ...muted, lineHeight: 1.4 } }, ui.ready)
            : null,
          open && finished
            ? detailList(mine, chatScoped, null, false, false, fullView, () => setFullView(true))
            : null,
          pairPanel(),
          renderOthersPanel(),
          noticeBlock(),
          fileInputElement,
        )
      }

      const drift = active.targetDrift || {}
      const view = viewLabel(active)
      const preview = previewLabel(active)
      const deliverWhy = deliverDisabledReason(active, chatScoped, busy)
      const previewWhy = previewDisabledReason(chatScoped, busy)
      const convertible = hasConvertibleContent(active)

      return h(
        'div',
        { style: { ...box, display: 'grid', gap: '6px' } },
        h(
          'div',
          { style: headerRow },
          h('span', { style: { fontWeight: 700, flex: 'none' } }, ui.brand),
          h('span', { style: { ...chip, flex: 'none' } }, active.pair),
          h(
            'span',
            {
              style: { ...pathChip, minWidth: 0, flex: '1 1 12ch' },
              title: active.targetPath,
            },
            shortPath(active.targetPath),
          ),
          h(StatusPill, { tone: view.tone, text: view.text }),
          preview
            ? h(StatusPill, {
                tone: 'warn',
                text: ui.statusCandidate,
                title: preview,
              })
            : null,
          drift.drifted ? h(StatusPill, { tone: 'error', text: ui.statusDrifted }) : null,
          active.active === false
            ? h(StatusPill, {
                tone: 'idle',
                text:
                  active.dirty || active.previewCandidate ? ui.idleDraft : ui.released,
              })
            : null,
          otherChipButton,
          pairChip,
          activityChip,
          sessionSelect,
          h(
            'div',
            {
              style: {
                display: 'flex',
                gap: '4px',
                alignItems: 'center',
                marginLeft: 'auto',
                flex: 'none',
              },
            },
            busy ? h('span', { style: { ...faint, whiteSpace: 'nowrap' } }, ui.working) : null,
            openFileIconButton,
            refreshButton,
            h(
              'button',
              {
                type: 'button',
                style: buttonStyle(quickButton, Boolean(previewWhy)),
                disabled: Boolean(previewWhy),
                title:
                  previewWhy ||
                  (convertible ? ui.previewHint : ui.previewHintNoConvertible),
                onClick: () => onAction('preview', active),
              },
              ui.preview,
            ),
            h(
              'button',
              {
                type: 'button',
                style: buttonStyle(primaryButton, Boolean(deliverWhy)),
                disabled: Boolean(deliverWhy),
                title: deliverWhy || ui.deliverHint,
                onClick: () => onAction('commit', active),
              },
              ui.deliver,
            ),
            detailsButton,
          ),
        ),
        confirmPanel,
        noticeBlock(),
        pairPanel(),
        renderOthersPanel(),
        open
          ? detailList(mine, chatScoped, null, false, false, fullView, () => setFullView(true))
          : null,
        fileInputElement,
      )
    }

    return {
      inject: ['slots'],
      apply(ctx) {
        ctx.slots.inject('conversation.composer.dock', () =>
          ctx.slots.register(
            {
              name: 'conversation.composer.dock',
              id: 'rosetta-status',
              order: 20,
            },
            RosettaStatus,
          ),
        )
      },
    }
  },
})

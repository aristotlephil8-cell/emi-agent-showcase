import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  consumeRunStream,
  fetchCases,
  fetchEvaluationSummary,
  fetchRun,
  isRecord,
  stringArray,
  stringValue,
  submitDecision,
} from './api'
import {
  evaluationSummaryRoot,
  formatCostDistribution,
  formatMetric,
  metricNumber,
  metricValue,
  variantRecord,
} from './evaluation'
import {
  parseCandidateRootCauses,
  parseEvidenceRecords,
  parseReviewIssues,
  unwrapRunSnapshot,
} from './contracts'
import type {
  AgentPhase,
  CandidateRootCause,
  CaseRecord,
  EvidenceItem,
  ReviewIssue,
  RunMode,
  StreamEvent,
  UnknownRecord,
} from './types'

const AGENTS = [
  { key: 'planner', label: 'PlannerAgent', caption: '拆解假设与证据计划', index: '01' },
  { key: 'evidence', label: 'Evidence Workers', caption: '白名单工具并行取证', index: '02' },
  { key: 'diagnosis', label: 'DiagnosisAgent', caption: '根因排序与置信边界', index: '03' },
  { key: 'reviewer', label: 'ReviewerAgent', caption: '证据审查与定向返工', index: '04' },
  { key: 'finalize', label: 'Finalize', caption: '确定性格式化报告', index: '05' },
] as const

const EMPTY_PHASES: Record<string, AgentPhase> = Object.fromEntries(
  AGENTS.map((agent) => [agent.key, 'idle']),
)

const METRIC_LABELS: Array<[string, string, 'higher' | 'lower']> = [
  ['plan_executable_rate', '计划可执行率', 'higher'],
  ['invalid_step_rate_micro', '无效步骤率（微平均）', 'lower'],
  ['invalid_step_rate_macro', '无效步骤率（宏平均）', 'lower'],
  ['task_completion_rate', '任务完成率', 'higher'],
  ['top1_root_cause_hit_rate', 'Top-1 根因命中率', 'higher'],
  ['unsupported_or_contradicted_claim_rate', '无依据或矛盾结论率', 'lower'],
  ['reviewer_resolution_rate', 'Reviewer 解决率', 'higher'],
  ['fault_recovery_rate', '故障恢复率', 'higher'],
]

function nodeKey(node?: string): string | undefined {
  const value = (node ?? '').toLowerCase()
  if (value.includes('planner')) return 'planner'
  if (value.includes('evidence') || value.includes('worker') || value.includes('router')) return 'evidence'
  if (value.includes('diagnos')) return 'diagnosis'
  if (value.includes('review')) return 'reviewer'
  if (value.includes('final') || value.includes('report')) return 'finalize'
  return undefined
}

function eventPhase(type: string): AgentPhase | undefined {
  const value = type.toLowerCase()
  if (value.includes('error') || value.includes('fail')) return 'error'
  if (value.includes('rework') || value.includes('retry')) return 'rework'
  if (value.includes('complete') || value.includes('finish') || value.includes('end') || value === 'report_ready') return 'completed'
  if (value.includes('start') || value.includes('progress') || value.includes('stream')) return 'running'
  return undefined
}

function displayTime(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '--:--:--' : date.toLocaleTimeString('zh-CN', { hour12: false })
}

function displayEventType(value: string): string {
  const labels: Record<string, string> = {
    node_start: '节点启动',
    node_end: '节点完成',
    tool_start: '工具调用',
    tool_result: '证据返回',
    rework_requested: '定向返工',
    final_report: '报告完成',
    run_completed: '运行完成',
    run_failed: '运行失败',
  }
  return labels[value] ?? value.replaceAll('_', ' ')
}

function payloadSummary(payload: UnknownRecord): string {
  for (const key of ['message', 'summary', 'finding', 'description', 'status', 'detail']) {
    const value = stringValue(payload[key])
    if (value) return value
  }
  const keys = Object.keys(payload).filter((key) => !['case', 'trajectory'].includes(key))
  return keys.length ? `更新：${keys.slice(0, 4).join('、')}` : '状态已更新'
}

function dedupeById<T extends { id: string }>(items: T[]): T[] {
  return Array.from(new Map(items.map((item) => [item.id, item])).values())
}

function App() {
  const [cases, setCases] = useState<CaseRecord[]>([])
  const [casesLoading, setCasesLoading] = useState(true)
  const [selectedCaseId, setSelectedCaseId] = useState('')
  const [mode, setMode] = useState<RunMode>('optimized')
  const [evaluation, setEvaluation] = useState<UnknownRecord>({})
  const [evaluationError, setEvaluationError] = useState('')
  const [running, setRunning] = useState(false)
  const [runId, setRunId] = useState('')
  const [runStatus, setRunStatus] = useState('idle')
  const [runError, setRunError] = useState('')
  const [phases, setPhases] = useState<Record<string, AgentPhase>>(EMPTY_PHASES)
  const [trajectory, setTrajectory] = useState<StreamEvent[]>([])
  const [evidence, setEvidence] = useState<EvidenceItem[]>([])
  const [candidates, setCandidates] = useState<CandidateRootCause[]>([])
  const [reviewIssues, setReviewIssues] = useState<ReviewIssue[]>([])
  const [decision, setDecision] = useState('accepted')
  const [decisionNotes, setDecisionNotes] = useState('')
  const [decisionState, setDecisionState] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle')
  const abortRef = useRef<AbortController | null>(null)

  const selectedCase = useMemo(
    () => cases.find((item) => item.id === selectedCaseId),
    [cases, selectedCaseId],
  )
  const selectedTopCause = useMemo(
    () => candidates.reduce<CandidateRootCause | undefined>(
      (best, item) => !best || item.rank < best.rank ? item : best,
      undefined,
    ),
    [candidates],
  )

  const loadEvaluation = useCallback(async () => {
    try {
      setEvaluation(await fetchEvaluationSummary())
      setEvaluationError('')
    } catch (error) {
      setEvaluationError(error instanceof Error ? error.message : '评测快照暂不可用')
    }
  }, [])

  useEffect(() => {
    let active = true
    fetchCases()
      .then((items) => {
        if (!active) return
        setCases(items)
        setSelectedCaseId((current) => current || items[0]?.id || '')
      })
      .catch((error: unknown) => {
        if (active) setRunError(error instanceof Error ? error.message : '案例列表加载失败')
      })
      .finally(() => {
        if (active) setCasesLoading(false)
      })
    void loadEvaluation()
    return () => {
      active = false
      abortRef.current?.abort()
    }
  }, [loadEvaluation])

  const ingestSnapshot = useCallback((snapshot: UnknownRecord) => {
    const runSnapshot = unwrapRunSnapshot(snapshot)
    const evidenceValue = runSnapshot.evidence ?? runSnapshot.evidence_items
    if (evidenceValue) setEvidence((current) => dedupeById([...current, ...parseEvidenceRecords(evidenceValue, 'snapshot')]))
    const diagnosis = runSnapshot.diagnosis ?? runSnapshot.root_causes ?? runSnapshot.candidates
    const parsedCandidates = parseCandidateRootCauses(diagnosis)
    if (parsedCandidates.length) setCandidates(parsedCandidates)
    const review = runSnapshot.review ?? runSnapshot.review_issues
    const parsedReview = parseReviewIssues(review)
    if (parsedReview.length) setReviewIssues(parsedReview)
    const status = stringValue(runSnapshot.status)
    if (status) setRunStatus(status)
  }, [])

  const onStreamEvent = useCallback((event: StreamEvent) => {
    setTrajectory((current) => [...current, event])
    if (event.run_id) setRunId(event.run_id)
    const key = nodeKey(event.node)
    const phase = eventPhase(event.type)
    if (key && phase) setPhases((current) => ({ ...current, [key]: phase }))

    const eventType = event.type.toLowerCase()
    const payload = event.payload
    if (key === 'evidence' || eventType.includes('evidence') || eventType.includes('tool_result')) {
      const value = payload.record ?? payload.evidence ?? payload.items ?? payload.result ?? payload
      setEvidence((current) => dedupeById([...current, ...parseEvidenceRecords(value, event.event_id)]))
    }
    if (key === 'diagnosis' || eventType.includes('diagnos')) {
      const parsed = parseCandidateRootCauses(
        payload.diagnosis ?? payload.root_causes ?? payload.candidates ?? payload,
      )
      if (parsed.length) setCandidates(parsed)
    }
    if (key === 'reviewer' || eventType.includes('review') || eventType.includes('rework')) {
      const parsed = parseReviewIssues(payload.review ?? payload.issues ?? payload)
      if (parsed.length) setReviewIssues(parsed)
    }
    if (eventType.includes('complete') || eventType.includes('final')) {
      const status = stringValue(payload.status, eventType.includes('run') ? 'completed' : '')
      if (status) setRunStatus(status)
    }
    if (eventType === 'report_ready' || eventType === 'final_report' || eventType === 'run_completed') {
      setPhases((current) => ({ ...current, finalize: 'completed' }))
    }
    if (eventType.includes('fail') || eventType.includes('error')) {
      setRunStatus('failed')
      setRunError(payloadSummary(payload))
    }
    ingestSnapshot(payload)
  }, [ingestSnapshot])

  const resetRun = () => {
    setRunId('')
    setRunStatus('running')
    setRunError('')
    setPhases(EMPTY_PHASES)
    setTrajectory([])
    setEvidence([])
    setCandidates([])
    setReviewIssues([])
    setDecisionNotes('')
    setDecisionState('idle')
  }

  const startRun = async () => {
    if (!selectedCaseId || running) return
    resetRun()
    setRunning(true)
    const controller = new AbortController()
    abortRef.current = controller
    let observedRunId = ''
    try {
      await consumeRunStream({
        body: { case_id: selectedCaseId, profile: mode },
        signal: controller.signal,
        onEvent: (event) => {
          if (event.run_id) observedRunId = event.run_id
          onStreamEvent(event)
        },
      })
      setRunStatus((current) => ['failed', 'needs_human_review'].includes(current) ? current : 'completed')
      if (observedRunId) {
        try {
          ingestSnapshot(await fetchRun(observedRunId))
        } catch {
          // The stream is authoritative; a delayed snapshot must not hide a completed run.
        }
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        setRunStatus('failed')
        setRunError(error instanceof Error ? error.message : '运行失败')
      }
    } finally {
      setRunning(false)
      abortRef.current = null
    }
  }

  const saveDecision = async () => {
    if (!runId || decisionState === 'saving') return
    setDecisionState('saving')
    try {
      await submitDecision(runId, decision, decisionNotes.trim(), selectedTopCause?.id)
      setDecisionState('saved')
    } catch {
      setDecisionState('error')
    }
  }

  const evaluationSummary = evaluationSummaryRoot(evaluation)
  const baseline = variantRecord(evaluation, 'baseline')
  const optimized = variantRecord(evaluation, 'optimized')
  const evaluationLabel = stringValue(
    evaluationSummary.label ?? evaluationSummary.evidence_level,
    'DEVELOPMENT_V1',
  )
  const canonicalLabels = stringArray(evaluationSummary.labels)
  const evaluationStatus = stringValue(evaluationSummary.status, 'not_run').toLowerCase()
  const evaluationComplete = evaluationSummary.complete === true
  const expectedEvaluation = isRecord(evaluationSummary.expected) ? evaluationSummary.expected : {}
  const evaluationCaseCount = typeof expectedEvaluation.cases === 'number'
    ? expectedEvaluation.cases
    : Array.isArray(expectedEvaluation.cases)
      ? expectedEvaluation.cases.length
      : typeof evaluationSummary.case_count === 'number'
        ? evaluationSummary.case_count
        : typeof evaluationSummary.total_cases === 'number'
          ? evaluationSummary.total_cases
          : undefined
  const evaluationNotRun = (!evaluationComplete && evaluationStatus === 'not_run')
    || Object.keys(baseline).length === 0
    || Object.keys(optimized).length === 0
  const evaluationLabels = Array.from(new Set([
    ...(canonicalLabels.length ? canonicalLabels : [evaluationLabel]),
    ...(evaluationNotRun ? ['NOT_RUN'] : []),
  ]))

  return (
    <main className="app-shell">
      <header className="topbar">
        <a className="brand" href="#top" aria-label="EMI Agent 首页">
          <span className="brand-mark" aria-hidden="true"><i /><i /><i /></span>
          <span>
            <strong>EMI AGENT</strong>
            <small>INTERFERENCE DECISION CONSOLE</small>
          </span>
        </a>
        <div className="header-meta">
          <span className="online-dot" />
          <span>LOCAL WORKSPACE</span>
          <span className="header-rule" />
          <span>v0.1</span>
        </div>
      </header>

      <section className="hero" id="top">
        <div className="hero-copy">
          <p className="eyebrow"><span>ENGINEERING INTELLIGENCE</span><b>公开合成数据</b></p>
          <h1>复杂设备电磁干扰<br /><em>多 Agent 辅助决策系统</em></h1>
          <p className="hero-summary">
            将问题拆解、并行取证、根因诊断与证据审查组织为可追踪工作流，
            为工程师提供带反证和边界的决策草案。
          </p>
        </div>
        <div className="hero-stamp" aria-label="证据等级">
          <span>EVIDENCE LEVEL</span>
          <strong>DEV·V1</strong>
          <small>NOT EXPERT VALIDATED</small>
        </div>
      </section>

      <section className="disclosure-strip" aria-label="重要说明">
        <span>DEVELOPMENT_V1</span>
        <span>FIXTURE_REPLAY_NOT_LIVE</span>
        <span>NOT_EXPERT_VALIDATED</span>
        <p>本项目使用公开合成数据，仅用于工程方法展示；系统提供辅助决策，不替代 EMC 专家判断。</p>
      </section>

      <section className="workspace-grid">
        <aside className="control-panel panel">
          <div className="section-heading">
            <span className="section-index">01</span>
            <div><p>RUN CONTROL</p><h2>运行控制</h2></div>
          </div>

          <label className="field-label" htmlFor="case-select">合成案例</label>
          <div className="select-wrap">
            <select
              id="case-select"
              value={selectedCaseId}
              onChange={(event) => setSelectedCaseId(event.target.value)}
              disabled={running || casesLoading}
            >
              {casesLoading && <option>正在加载案例…</option>}
              {!casesLoading && cases.length === 0 && <option value="">暂无可用案例</option>}
              {cases.map((item) => <option value={item.id} key={item.id}>{item.id} · {item.title}</option>)}
            </select>
          </div>

          {selectedCase && (
            <article className="case-card">
              <div className="case-card-top"><span>{selectedCase.category}</span><code>{selectedCase.id}</code></div>
              <h3>{selectedCase.title}</h3>
              <p>{selectedCase.description || selectedCase.symptom || '等待案例描述。'}</p>
              {selectedCase.observations.length > 0 && (
                <ul>{selectedCase.observations.slice(0, 3).map((item) => <li key={item}>{item}</li>)}</ul>
              )}
            </article>
          )}

          <label className="field-label">工作流模式</label>
          <div className="mode-switch" role="radiogroup" aria-label="工作流模式">
            {(['baseline', 'optimized'] as RunMode[]).map((item) => (
              <button
                type="button"
                role="radio"
                aria-checked={mode === item}
                className={mode === item ? 'active' : ''}
                onClick={() => setMode(item)}
                disabled={running}
                key={item}
              >
                <span>{item === 'baseline' ? '基线' : '优化'}</span>
                <small>{item === 'baseline' ? '串行 · 无审查' : '并行 · 审查返工'}</small>
              </button>
            ))}
          </div>

          <button className="run-button" type="button" onClick={() => void startRun()} disabled={!selectedCaseId || running}>
            <span>{running ? 'AGENTS RUNNING' : 'START ANALYSIS'}</span>
            <i aria-hidden="true">{running ? '•••' : '↗'}</i>
          </button>
          <div className="run-meta">
            <span className={`status-pill ${runStatus}`}>{runStatus === 'idle' ? '等待运行' : runStatus}</span>
            <span>{runId ? `RUN ${runId.slice(0, 8)}` : 'NO ACTIVE RUN'}</span>
          </div>
          {runError && <p className="inline-error" role="alert">{runError}</p>}
        </aside>

        <section className="execution-panel panel">
          <div className="section-heading heading-row">
            <div className="heading-group">
              <span className="section-index">02</span>
              <div><p>STREAMING ORCHESTRATION</p><h2>Agent 执行图</h2></div>
            </div>
            <span className={`live-state ${running ? 'is-live' : ''}`}><i />{running ? 'RUNNING' : 'STANDBY'}</span>
          </div>

          <div className="agent-dag" aria-label="Agent 工作流状态">
            {AGENTS.map((agent, index) => (
              <div className="agent-step-wrap" key={agent.key}>
                <article className={`agent-step phase-${phases[agent.key]}`}>
                  <div className="agent-number">{agent.index}</div>
                  <div className="agent-icon" aria-hidden="true"><span /></div>
                  <div className="agent-copy"><strong>{agent.label}</strong><small>{agent.caption}</small></div>
                  <span className="phase-label">{phases[agent.key]}</span>
                </article>
                {index < AGENTS.length - 1 && <div className="dag-arrow" aria-hidden="true"><span>→</span></div>}
              </div>
            ))}
          </div>

          <div className="trajectory-head">
            <div><span>EVENT STREAM</span><strong>实时轨迹</strong></div>
            <code>{trajectory.length.toString().padStart(2, '0')} EVENTS</code>
          </div>
          <div className="trajectory-list" aria-live="polite">
            {trajectory.length === 0 ? (
              <div className="empty-state"><span>⌁</span><p>选择案例并启动分析，节点事件将在此逐条到达。</p></div>
            ) : trajectory.map((event) => (
              <article className="trajectory-event" key={`${event.event_id}-${event.sequence}`}>
                <time>{displayTime(event.timestamp)}</time>
                <span className={`event-dot ${eventPhase(event.type) ?? ''}`} />
                <div><strong>{event.node || 'workflow'} · {displayEventType(event.type)}</strong><p>{payloadSummary(event.payload)}</p></div>
                <code>#{event.sequence}</code>
              </article>
            ))}
          </div>
        </section>
      </section>

      <section className="analysis-grid">
        <article className="panel evidence-panel">
          <div className="section-heading compact">
            <span className="section-index">03</span>
            <div><p>TRACEABLE EVIDENCE</p><h2>证据与反证</h2></div>
          </div>
          <div className="evidence-list">
            {evidence.length === 0 ? <Placeholder text="证据工具返回后展示来源、支持关系与反证。" /> : evidence.map((item) => (
              <article className="evidence-card" key={item.id}>
                <div className="evidence-top"><code>{item.tool}</code><span>{item.status}</span></div>
                <p>{item.finding}</p>
                <div className="evidence-relations">
                  <span className="support">支持 {item.supports.length || '—'}</span>
                  <span className={item.contradicts.length ? 'counter' : ''}>反证 {item.contradicts.length || '—'}</span>
                  {item.tags.slice(0, 2).map((tag) => <span className="evidence-tag" key={tag}>{tag}</span>)}
                </div>
              </article>
            ))}
          </div>
        </article>

        <article className="panel diagnosis-panel">
          <div className="section-heading compact">
            <span className="section-index">04</span>
            <div><p>ROOT CAUSE RANKING</p><h2>候选根因</h2></div>
          </div>
          <div className="cause-list">
            {candidates.length === 0 ? <Placeholder text="DiagnosisAgent 将输出可审计的根因排序与置信边界。" /> : [...candidates]
              .sort((a, b) => a.rank - b.rank)
              .map((item) => (
                <article className="cause-card" key={item.id}>
                  <span className="cause-rank">{String(item.rank).padStart(2, '0')}</span>
                  <div className="cause-copy">
                    <strong>{item.cause}</strong>
                    <p>{item.evidenceIds.length} 条证据 · {item.counterEvidence.length} 条反证</p>
                    {item.rationale && <small>{item.rationale}</small>}
                  </div>
                  <div className="confidence">
                    <span>{item.confidence === undefined ? '—' : `${Math.round(item.confidence * 100)}%`}</span>
                    <i style={{ '--confidence': `${Math.max(0, Math.min(1, item.confidence ?? 0)) * 100}%` } as React.CSSProperties} />
                  </div>
                </article>
              ))}
          </div>
        </article>

        <article className="panel review-panel">
          <div className="section-heading compact">
            <span className="section-index">05</span>
            <div><p>REVIEW & REWORK</p><h2>审查与定向返工</h2></div>
          </div>
          <div className="review-list">
            {reviewIssues.length === 0 ? <Placeholder text={mode === 'baseline' ? 'Baseline 模式不启用 Reviewer。' : 'Reviewer 尚未发现需要返工的问题。'} /> : reviewIssues.map((issue) => (
              <article className={`review-card ${issue.resolved ? 'resolved' : ''}`} key={issue.id}>
                <span>{issue.resolved ? '✓' : '!'}</span>
                <div><strong>{issue.description}</strong><p>{issue.severity.toUpperCase()} · {issue.kind.toUpperCase()} · TARGET {issue.target}</p></div>
              </article>
            ))}
          </div>
          <div className="decision-box">
            <div><span>HUMAN IN THE LOOP</span><strong>工程师确认</strong></div>
            <select value={decision} onChange={(event) => setDecision(event.target.value)} disabled={!runId} aria-label="工程师决策">
              <option value="accepted">确认并接受建议</option>
              <option value="modified">要求补证或修改</option>
              <option value="rejected">拒绝建议</option>
            </select>
            {selectedTopCause && <p className="decision-target">关联 Top-1：{selectedTopCause.cause}</p>}
            <textarea
              value={decisionNotes}
              onChange={(event) => setDecisionNotes(event.target.value)}
              placeholder="记录工程判断或后续验证条件（可选）"
              disabled={!runId}
              rows={3}
            />
            <button type="button" onClick={() => void saveDecision()} disabled={!runId || decisionState === 'saving'}>
              {decisionState === 'saving' ? '保存中…' : decisionState === 'saved' ? '已记录 ✓' : '记录工程师决策'}
            </button>
            {decisionState === 'error' && <p className="inline-error">决策保存失败，请稍后重试。</p>}
          </div>
        </article>
      </section>

      <section className="evaluation-section panel">
        <div className="section-heading heading-row">
          <div className="heading-group">
            <span className="section-index">06</span>
            <div><p>REPRODUCIBLE EVALUATION</p><h2>固定评测快照</h2></div>
          </div>
          <div className="evaluation-labels">
            {evaluationLabels.map((label) => <span key={label}>{label}</span>)}
            {!evaluationNotRun && <span>{evaluationCaseCount ?? 24} SYNTHETIC CASES</span>}
          </div>
        </div>
        {evaluationError ? (
          <div className="evaluation-error"><p>评测快照暂不可用：{evaluationError}</p><button type="button" onClick={() => void loadEvaluation()}>重新加载</button></div>
        ) : (
          <>
            {evaluationNotRun && (
              <div className="not-run-notice">
                <strong>LIVE EVALUATION NOT RUN</strong>
                <p>当前仓库尚无可核验的真实模型配对结果，因此不展示占位比例；完成评测并生成 summary 后此表会自动更新。</p>
              </div>
            )}
            <div className="metric-table" role="table" aria-label="Baseline 与 Optimized 评测对照">
              <div className="metric-row metric-header" role="row">
                <span>METRIC / 指标</span><span>BASELINE</span><span>OPTIMIZED</span><span>方向</span>
              </div>
              {METRIC_LABELS.map(([key, label, direction]) => {
                const baselineValue = metricValue(baseline, key)
                const optimizedValue = metricValue(optimized, key)
                const baseNumber = metricNumber(baselineValue)
                const optimizedNumber = metricNumber(optimizedValue)
                const improved = baseNumber !== undefined && optimizedNumber !== undefined
                  ? direction === 'higher' ? optimizedNumber > baseNumber : optimizedNumber < baseNumber
                  : false
                return (
                  <div className="metric-row" role="row" key={key}>
                    <strong>{label}</strong>
                    <span>{formatMetric(baselineValue)}</span>
                    <span className={improved ? 'metric-improved' : ''}>{formatMetric(optimizedValue)}</span>
                    <span>{direction === 'higher' ? '↑ 越高越好' : '↓ 越低越好'}</span>
                  </div>
                )
              })}
            </div>
          </>
        )}
        <div className="cost-comparison" role="table" aria-label="Baseline 与 Optimized 成本分布对照">
          <div className="cost-row cost-header" role="row">
            <span>COST DISTRIBUTION / 单案例分布</span><span>BASELINE</span><span>OPTIMIZED</span>
          </div>
          {([
            ['模型调用 · 均值/范围', 'model_calls', 'mean', ''],
            ['输入 Token · 均值/范围', 'input_tokens', 'mean', ''],
            ['输出 Token · 均值/范围', 'output_tokens', 'mean', ''],
            ['端到端时延 · 中位数/范围', 'latency_median_ms', 'median', 'ms'],
          ] as const).map(([label, key, statistic, unit]) => (
            <div className="cost-row" role="row" key={key}>
              <strong>{label}</strong>
              <span>{formatCostDistribution(metricValue(baseline, key), statistic, unit)}</span>
              <span>{formatCostDistribution(metricValue(optimized, key), statistic, unit)}</span>
            </div>
          ))}
        </div>
        <p className="evaluation-note">
          单次配对开发评测，不代表统计显著性、生产稳定性或专家认可；所有比例应以仓库原始轨迹和可重算评分明细为准。
        </p>
      </section>

      <footer>
        <span>EMI AGENT · ENGINEERING SHOWCASE</span>
        <p>公开合成案例 / 辅助决策 / 非生产系统</p>
      </footer>
    </main>
  )
}

function Placeholder({ text }: { text: string }) {
  return <div className="panel-placeholder"><span>◇</span><p>{text}</p></div>
}

export default App

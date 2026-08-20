export type ContractRecord = Record<string, unknown>

import type { CandidateRootCause, EvidenceItem, ReviewIssue } from './types'

function isRecord(value: unknown): value is ContractRecord {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

export function unwrapRunSnapshot(snapshot: ContractRecord): ContractRecord {
  const nested = isRecord(snapshot.benchmark)
    ? snapshot.benchmark
    : isRecord(snapshot.state)
      ? snapshot.state
      : snapshot
  return nested === snapshot ? snapshot : unwrapRunSnapshot(nested)
}

function stringValue(value: unknown, fallback = ''): string {
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return fallback
}

function recordText(value: unknown): string {
  if (!isRecord(value)) return stringValue(value)
  for (const key of ['observation', 'finding', 'message', 'label', 'value', 'text', 'description']) {
    const text = stringValue(value[key])
    if (text) return text
  }
  return ''
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return value === undefined || value === null ? [] : [recordText(value)].filter(Boolean)
  return value.map(recordText).filter(Boolean)
}

function recordArray(value: unknown, keys: string[]): ContractRecord[] {
  if (Array.isArray(value)) return value.filter(isRecord)
  if (!isRecord(value)) return []
  for (const key of keys) {
    const nested = value[key]
    if (Array.isArray(nested)) return nested.filter(isRecord)
    if (isRecord(nested)) return [nested]
  }
  return [value]
}

export function parseEvidenceRecords(value: unknown, seed: string): EvidenceItem[] {
  const source = isRecord(value) && value.record !== undefined ? value.record : value
  return recordArray(source, ['records', 'evidence', 'items'])
    .filter((item) => [
      item.operation_id,
      item.evidence_id,
      item.id,
      item.finding,
      item.observations,
      item.tool,
      item.tool_name,
    ].some((field) => field !== undefined))
    .map((item, index) => {
    const observations = stringList(item.observations)
    return {
      id: stringValue(item.operation_id ?? item.evidence_id ?? item.id, `${seed}-${index}`),
      tool: stringValue(item.tool ?? item.tool_name ?? item.source ?? item.kind, 'evidence_tool'),
      finding: stringValue(
        item.finding ?? item.content ?? item.summary ?? item.result,
        observations.join('；') || '已返回结构化证据',
      ),
      supports: stringList(
        item.supports_claim_ids ?? item.supports ?? item.supported_claims ?? item.hypotheses,
      ),
      contradicts: stringList(
        item.contradicts_claim_ids ?? item.contradicts ?? item.counterevidence ?? item.counter_evidence,
      ),
      tags: stringList(item.evidence_tags ?? item.tags),
      status: stringValue(item.status, 'verified'),
    }
    })
}

export function parseCandidateRootCauses(value: unknown): CandidateRootCause[] {
  const container = isRecord(value) && isRecord(value.diagnosis) ? value.diagnosis : value
  return recordArray(container, ['root_causes', 'candidates'])
    .filter((item) => [
      item.root_cause_id,
      item.cause_id,
      item.id,
      item.label,
      item.cause,
      item.root_cause,
      item.title,
      item.hypothesis,
    ].some((field) => field !== undefined))
    .map((item, index) => ({
    id: stringValue(item.root_cause_id ?? item.cause_id ?? item.id, `cause-${index + 1}`),
    rank: typeof item.rank === 'number' ? item.rank : index + 1,
    cause: stringValue(
      item.label ?? item.cause ?? item.root_cause ?? item.title ?? item.hypothesis,
      '未命名候选根因',
    ),
    rationale: stringValue(item.rationale ?? item.reasoning ?? item.summary) || undefined,
    confidence:
      typeof item.confidence === 'number'
        ? item.confidence
        : typeof item.score === 'number'
          ? item.score
          : undefined,
    evidenceIds: stringList(
      item.evidence_ids ?? item.supporting_evidence_ids ?? item.supporting_evidence,
    ),
    counterEvidence: stringList(
      item.counter_evidence_ids ?? item.counter_evidence ?? item.counterevidence ?? item.contradictions,
    ),
    }))
}

export function parseReviewIssues(value: unknown): ReviewIssue[] {
  const container = isRecord(value) && isRecord(value.review) ? value.review : value
  return recordArray(container, ['issues'])
    .filter((item) => [
      item.id,
      item.issue_id,
      item.message,
      item.description,
      item.issue,
      item.reason,
      item.kind,
      item.issue_type,
    ].some((field) => field !== undefined))
    .map((item, index) => ({
    id: stringValue(item.id ?? item.issue_id, `issue-${index + 1}`),
    severity: stringValue(item.severity, 'medium'),
    kind: stringValue(item.kind ?? item.issue_type, 'evidence_gap'),
    description: stringValue(
      item.message ?? item.description ?? item.issue ?? item.reason,
      'Reviewer 标记了证据缺口',
    ),
    target: stringValue(item.target_id ?? item.target ?? item.target_step ?? item.step_id, 'evidence'),
    resolved: item.resolved === true || item.status === 'resolved',
    }))
}

export function buildDecisionRequest(
  decision: string,
  notes: string,
  selectedCauseId?: string,
): ContractRecord {
  return {
    decision,
    notes,
    ...(selectedCauseId ? { selected_cause_id: selectedCauseId } : {}),
  }
}

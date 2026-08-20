import type { CaseRecord, StreamEvent, UnknownRecord } from './types'
import { buildDecisionRequest } from './contracts'

const API_ROOT = '/api/v1'

function isRecord(value: unknown): value is UnknownRecord {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function stringValue(value: unknown, fallback = ''): string {
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return fallback
}

function stringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.map((item) => stringValue(item)).filter(Boolean)
}

async function expectJson(response: Response): Promise<unknown> {
  if (!response.ok) {
    const message = await response.text()
    throw new Error(message || `请求失败（HTTP ${response.status}）`)
  }
  return response.json() as Promise<unknown>
}

export async function fetchCases(): Promise<CaseRecord[]> {
  const payload = await expectJson(await fetch(`${API_ROOT}/cases`))
  const source = Array.isArray(payload)
    ? payload
    : isRecord(payload) && Array.isArray(payload.cases)
      ? payload.cases
      : []

  return source.filter(isRecord).map((item, index) => {
    const id = stringValue(item.case_id ?? item.id, `case-${index + 1}`)
    return {
      id,
      title: stringValue(item.title ?? item.name, `合成案例 ${index + 1}`),
      category: stringValue(item.category ?? item.interference_type, '未分类'),
      description: stringValue(item.description ?? item.context ?? item.summary),
      symptom: stringValue(item.symptom ?? item.observed_issue),
      observations: stringArray(item.observations ?? item.measurements ?? item.signals),
      raw: item,
    }
  })
}

export async function fetchEvaluationSummary(): Promise<UnknownRecord> {
  const payload = await expectJson(await fetch(`${API_ROOT}/evaluation/summary`))
  return isRecord(payload) ? payload : {}
}

export async function fetchRun(runId: string): Promise<UnknownRecord> {
  const payload = await expectJson(await fetch(`${API_ROOT}/runs/${encodeURIComponent(runId)}`))
  return isRecord(payload) ? payload : {}
}

export async function submitDecision(
  runId: string,
  decision: string,
  notes: string,
  selectedCauseId?: string,
): Promise<UnknownRecord> {
  const response = await fetch(`${API_ROOT}/runs/${encodeURIComponent(runId)}/decision`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(buildDecisionRequest(decision, notes, selectedCauseId)),
  })
  const payload = await expectJson(response)
  return isRecord(payload) ? payload : {}
}

interface StreamOptions {
  path?: string
  body: UnknownRecord
  signal?: AbortSignal
  onEvent: (event: StreamEvent) => void
}

function normalizeEvent(value: unknown, eventName: string, fallbackSequence: number): StreamEvent {
  const item = isRecord(value) ? value : {}
  const payload = isRecord(item.payload)
    ? item.payload
    : isRecord(item.data)
      ? item.data
      : item
  return {
    event_id: stringValue(item.event_id, `local-${fallbackSequence}`),
    run_id: stringValue(item.run_id) || undefined,
    attempt_id: stringValue(item.attempt_id) || undefined,
    sequence: typeof item.sequence === 'number' ? item.sequence : fallbackSequence,
    type: stringValue(item.type, eventName || 'message'),
    node: stringValue(item.node) || undefined,
    payload,
    timestamp: stringValue(item.timestamp, new Date().toISOString()),
  }
}

export async function consumeRunStream({
  path = `${API_ROOT}/runs/stream`,
  body,
  signal,
  onEvent,
}: StreamOptions): Promise<void> {
  const response = await fetch(path, {
    method: 'POST',
    headers: {
      Accept: 'text/event-stream',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(body),
    signal,
  })

  if (!response.ok) {
    const message = await response.text()
    throw new Error(message || `运行请求失败（HTTP ${response.status}）`)
  }
  if (!response.body) throw new Error('浏览器未收到可读取的事件流。')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let sequence = 0

  const dispatch = (block: string) => {
    let eventName = 'message'
    const dataLines: string[] = []
    block.split(/\r?\n/).forEach((line) => {
      if (line.startsWith('event:')) eventName = line.slice(6).trim()
      if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart())
    })
    if (dataLines.length === 0) return
    const raw = dataLines.join('\n')
    if (raw === '[DONE]') return
    sequence += 1
    try {
      onEvent(normalizeEvent(JSON.parse(raw) as unknown, eventName, sequence))
    } catch {
      onEvent(normalizeEvent({ type: eventName, payload: { message: raw } }, eventName, sequence))
    }
  }

  while (true) {
    const { done, value } = await reader.read()
    buffer += decoder.decode(value, { stream: !done }).replace(/\r\n/g, '\n')
    let boundary = buffer.indexOf('\n\n')
    while (boundary >= 0) {
      dispatch(buffer.slice(0, boundary))
      buffer = buffer.slice(boundary + 2)
      boundary = buffer.indexOf('\n\n')
    }
    if (done) break
  }
  if (buffer.trim()) dispatch(buffer)
}

export { isRecord, stringArray, stringValue }

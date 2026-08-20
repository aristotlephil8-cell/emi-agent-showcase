export type RunMode = 'baseline' | 'optimized'
export type AgentPhase = 'idle' | 'running' | 'completed' | 'rework' | 'error'

export interface CaseRecord {
  id: string
  title: string
  category: string
  description: string
  symptom?: string
  observations: string[]
  raw: Record<string, unknown>
}

export interface StreamEvent {
  event_id: string
  run_id?: string
  attempt_id?: string
  sequence: number
  type: string
  node?: string
  payload: Record<string, unknown>
  timestamp: string
}

export interface EvidenceItem {
  id: string
  tool: string
  finding: string
  supports: string[]
  contradicts: string[]
  tags: string[]
  status: string
}

export interface CandidateRootCause {
  id: string
  rank: number
  cause: string
  rationale?: string
  confidence?: number
  evidenceIds: string[]
  counterEvidence: string[]
}

export interface ReviewIssue {
  id: string
  severity: string
  kind: string
  description: string
  target: string
  resolved: boolean
}

export type UnknownRecord = Record<string, unknown>

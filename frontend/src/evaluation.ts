export type EvaluationRecord = Record<string, unknown>

const METRIC_ALIASES: Record<string, string[]> = {
  plan_executable_rate: ['plan_executable_rate'],
  invalid_step_rate_micro: ['invalid_step_rate_micro', 'invalid_step_rate'],
  invalid_step_rate_macro: ['invalid_step_rate_macro'],
  task_completion_rate: ['task_completion_rate', 'completion_rate'],
  top1_root_cause_hit_rate: ['top1_root_cause_hit_rate', 'top1_root_cause_accuracy'],
  unsupported_or_contradicted_claim_rate: [
    'unsupported_or_contradicted_claim_rate',
    'unsupported_claim_rate',
  ],
  reviewer_resolution_rate: ['reviewer_resolution_rate'],
  fault_recovery_rate: ['fault_recovery_rate'],
  model_calls: ['model_calls', 'model_call_count'],
  input_tokens: ['input_tokens', 'prompt_tokens'],
  output_tokens: ['output_tokens', 'completion_tokens'],
  latency_median_ms: ['latency_median_ms', 'median_latency_ms', 'latency_ms'],
}

function isRecord(value: unknown): value is EvaluationRecord {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

export function evaluationSummaryRoot(evaluation: EvaluationRecord): EvaluationRecord {
  return isRecord(evaluation.summary) ? evaluation.summary : evaluation
}

export function variantRecord(
  evaluation: EvaluationRecord,
  variant: 'baseline' | 'optimized',
): EvaluationRecord {
  const summary = evaluationSummaryRoot(evaluation)
  if (isRecord(summary.variants) && isRecord(summary.variants[variant])) {
    return summary.variants[variant]
  }
  if (isRecord(summary[variant])) return summary[variant]
  if (isRecord(summary.metrics) && isRecord(summary.metrics[variant])) {
    return summary.metrics[variant]
  }
  return {}
}

export function metricValue(source: EvaluationRecord, canonicalKey: string): unknown {
  const aliases = METRIC_ALIASES[canonicalKey] ?? [canonicalKey]
  const containers = [source, source.metrics, source.cost].filter(isRecord)
  for (const container of containers) {
    for (const key of aliases) {
      if (container[key] !== undefined) return container[key]
    }
  }
  return undefined
}

export function metricNumber(value: unknown): number | undefined {
  if (typeof value === 'number') return value
  if (isRecord(value)) {
    if (typeof value.value === 'number') return value.value
    if (typeof value.rate === 'number') return value.rate
    if (
      typeof value.numerator === 'number'
      && typeof value.denominator === 'number'
      && value.denominator !== 0
    ) {
      return value.numerator / value.denominator
    }
  }
  return undefined
}

export function formatMetric(value: unknown): string {
  if (value === undefined || value === null) return '—'
  if (typeof value === 'string') return value
  const number = metricNumber(value)
  if (number === undefined) return '—'
  const count = isRecord(value)
    && typeof value.numerator === 'number'
    && typeof value.denominator === 'number'
    ? `${value.numerator}/${value.denominator} · `
    : ''
  return `${count}${(number * 100).toFixed(1)}%`
}

function formatDecimal(value: number): string {
  return new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 1 }).format(value)
}

export function formatCostDistribution(
  value: unknown,
  statistic: 'mean' | 'median',
  unit = '',
): string {
  const suffix = unit ? ` ${unit}` : ''
  if (typeof value === 'number') return `${formatDecimal(value)}${suffix}`
  if (!isRecord(value)) return '—'
  const center = typeof value[statistic] === 'number'
    ? value[statistic]
    : typeof value.mean === 'number'
      ? value.mean
      : undefined
  if (center === undefined) return '—'
  const bounds = typeof value.min === 'number' && typeof value.max === 'number'
    ? ` · ${formatDecimal(value.min)}–${formatDecimal(value.max)}${suffix}`
    : ''
  return `${formatDecimal(center)}${suffix}${bounds}`
}

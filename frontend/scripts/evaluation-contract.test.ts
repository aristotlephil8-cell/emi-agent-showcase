import {
  evaluationSummaryRoot,
  formatCostDistribution,
  metricValue,
  variantRecord,
  type EvaluationRecord,
} from '../src/evaluation.ts'
import {
  buildDecisionRequest,
  parseCandidateRootCauses,
  parseEvidenceRecords,
  parseReviewIssues,
  unwrapRunSnapshot,
} from '../src/contracts.ts'

function assertEqual(actual: unknown, expected: unknown, label: string): void {
  if (actual !== expected) {
    throw new Error(`${label}: expected ${String(expected)}, received ${String(actual)}`)
  }
}

const scorerFixture: EvaluationRecord = {
  summary: {
    variants: {
      baseline: {
        invalid_step_rate_micro: 0.25,
        invalid_step_rate_macro: 0.2,
        top1_root_cause_hit_rate: { numerator: 12, denominator: 24, value: 0.5 },
        unsupported_or_contradicted_claim_rate: 0.3,
        cost: {
          model_calls: { count: 24, total: 96, mean: 4, median: 4, min: 3, max: 6, range: 3 },
          input_tokens: { count: 24, total: 12000, mean: 500, median: 470, min: 320, max: 720, range: 400 },
          latency_ms: { count: 24, total: 30000, mean: 1250, median: 1180, min: 900, max: 1800, range: 900 },
        },
      },
      optimized: {
        invalid_step_rate_micro: 0.1,
        top1_root_cause_hit_rate: 0.75,
      },
    },
  },
}

const summary = evaluationSummaryRoot(scorerFixture)
const baseline = variantRecord(scorerFixture, 'baseline')
const optimized = variantRecord(scorerFixture, 'optimized')

assertEqual(typeof summary.variants, 'object', 'summary wrapper')
assertEqual(metricValue(baseline, 'invalid_step_rate_micro'), 0.25, 'micro invalid-step rate')
assertEqual(metricValue(baseline, 'invalid_step_rate_macro'), 0.2, 'macro invalid-step rate')
assertEqual(
  formatCostDistribution(metricValue(baseline, 'model_calls'), 'mean'),
  '4 · 3–6',
  'model-call distribution',
)
assertEqual(
  formatCostDistribution(metricValue(baseline, 'latency_median_ms'), 'median', 'ms'),
  '1,180 ms · 900–1,800 ms',
  'latency distribution',
)
assertEqual(metricValue(optimized, 'top1_root_cause_hit_rate'), 0.75, 'top-1 hit rate')

const compatibilityFixture: EvaluationRecord = {
  baseline: { invalid_step_rate: 0.4, top1_root_cause_accuracy: 0.45 },
}
const compatibilityBaseline = variantRecord(compatibilityFixture, 'baseline')
assertEqual(metricValue(compatibilityBaseline, 'invalid_step_rate_micro'), 0.4, 'legacy invalid-step alias')
assertEqual(metricValue(compatibilityBaseline, 'top1_root_cause_hit_rate'), 0.45, 'legacy top-1 alias')

const runSnapshot = unwrapRunSnapshot({ benchmark: { state: { status: 'completed', evidence: [{ id: 'e-1' }] } } })
assertEqual(runSnapshot.status, 'completed', 'nested run snapshot status')
assertEqual(Array.isArray(runSnapshot.evidence), true, 'nested run snapshot evidence')

const evidenceSsePayload = {
  record: {
    operation_id: 'op-frequency-01',
    tool_name: 'frequency_signature_match',
    observations: ['150 MHz 与时钟三次谐波重合', '近场探头在连接器处达到峰值'],
    supports_claim_ids: ['claim-clock-harmonic'],
    contradicts_claim_ids: ['claim-power-ripple'],
    evidence_tags: ['frequency_match', 'near_field'],
  },
}
const parsedEvidence = parseEvidenceRecords(evidenceSsePayload, 'evt-12')
assertEqual(parsedEvidence[0]?.id, 'op-frequency-01', 'SSE evidence operation id')
assertEqual(parsedEvidence[0]?.supports[0], 'claim-clock-harmonic', 'SSE supports claim ids')
assertEqual(parsedEvidence[0]?.contradicts[0], 'claim-power-ripple', 'SSE contradicts claim ids')
assertEqual(parsedEvidence[0]?.tags[1], 'near_field', 'SSE evidence tags')

const benchmarkPayload = unwrapRunSnapshot({
  benchmark: {
    diagnosis: {
      root_causes: [{
        root_cause_id: 'rc-clock-return-01',
        rank: 1,
        label: '时钟回流路径跨越屏蔽缝隙',
        rationale: '频率指纹和近场峰值同时支持该耦合路径。',
        confidence: 0.82,
        evidence_ids: ['op-frequency-01'],
      }],
    },
    review: {
      issues: [{
        issue_id: 'review-01',
        message: '需要补充接口滤波器干预前后对照。',
        kind: 'evidence_gap',
        issue_type: 'missing_counterfactual',
        target_id: 'step-interface-filter',
        severity: 'high',
      }],
    },
  },
})
const parsedCauses = parseCandidateRootCauses(benchmarkPayload.diagnosis)
const parsedReview = parseReviewIssues(benchmarkPayload.review)
assertEqual(parsedCauses[0]?.id, 'rc-clock-return-01', 'benchmark root_cause_id')
assertEqual(parsedCauses[0]?.cause, '时钟回流路径跨越屏蔽缝隙', 'benchmark root-cause label')
assertEqual(parsedCauses[0]?.rationale, '频率指纹和近场峰值同时支持该耦合路径。', 'benchmark rationale')
assertEqual(parsedReview[0]?.description, '需要补充接口滤波器干预前后对照。', 'review message')
assertEqual(parsedReview[0]?.kind, 'evidence_gap', 'review kind')
assertEqual(parsedReview[0]?.target, 'step-interface-filter', 'review target_id')

const issueTypeOnly = parseReviewIssues({ issues: [{ issue_type: 'unsupported_claim', message: '结论缺少证据。' }] })
assertEqual(issueTypeOnly[0]?.kind, 'unsupported_claim', 'review issue_type fallback')

const decisionRequest = buildDecisionRequest('accepted', '工程师确认', parsedCauses[0]?.id)
assertEqual(decisionRequest.selected_cause_id, 'rc-clock-return-01', 'decision uses real root-cause id')

console.log('frontend contract fixtures: PASS')

/**
 * 数字回溯证据的展示辅助：类型中文标签、证据一句话描述、数字格式化。
 * 供答案内数字标注（hover 提示）与证据抽屉共用。
 */
import type { EvidenceLocation, NumberVerdict } from '../api/types'

export const KIND_LABELS: Record<string, string> = {
  number: '数值',
  percent: '百分比',
  multiplier: '倍数',
  currency: '金额',
  count: '计数',
  ordinal: '序数',
  chinese_number: '中文数字',
}

export function kindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? '数字'
}

/** 300.0 -> "300"；116.6666 -> "116.6667"，避免一长串浮点尾数 */
export function formatNumber(value: number): string {
  if (Number.isInteger(value)) return String(value)
  return String(Math.round(value * 1_000_000) / 1_000_000)
}

function evidenceValueText(loc: EvidenceLocation): string | null {
  if (loc.evidence_value === undefined || loc.evidence_value === null) {
    return null
  }
  return formatNumber(loc.evidence_value)
}

/** 证据一句话摘要（也用作答案数字的 hover 提示） */
export function evidenceSummary(verdict: NumberVerdict): string {
  if (!verdict.found || !verdict.evidence) {
    return '未在执行结果中找到该数字的证据（疑似幻觉）'
  }
  const loc = verdict.evidence

  // 表格单元格：{row, column, evidence_value}（无 type 字段）
  if (typeof loc.row === 'number' && loc.column !== undefined) {
    const valueText = evidenceValueText(loc)
    return (
      `证据：结果表第 ${loc.row + 1} 行「${loc.column}」列` +
      (valueText !== null ? `，单元格值 ${valueText}` : '')
    )
  }

  if (loc.type === 'table_meta' && loc.matched === 'row_rank') {
    return `证据：结果共 ${loc.total_rows ?? 0} 行，排名第 ${loc.rank ?? '?'} 存在`
  }

  if (loc.type === 'table_meta' && loc.matched === 'row_count') {
    return `证据：结果共 ${loc.total_rows ?? 0} 行，计数 ${loc.count ?? '?'} 与行数一致`
  }

  if (loc.type === 'single_value') {
    const valueText = evidenceValueText(loc)
    return `证据：标量结果，值为 ${valueText ?? '空'}`
  }

  if (loc.type === 'record') {
    const valueText = evidenceValueText(loc)
    return (
      `证据：结果字段「${loc.key ?? ''}」` +
      (valueText !== null ? `，值为 ${valueText}` : '')
    )
  }

  return '证据：已在执行结果中回溯到'
}

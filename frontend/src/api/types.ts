/**
 * 后端 /api/v1 接口的 TS 类型定义，与 FastAPI 响应契约一一对应。
 */

/** 数据源列类型（schema extractor 只会产出这四种） */
export type ColumnDtype = 'numeric' | 'categorical' | 'datetime' | 'empty'

/** 上传响应里的精简列信息 */
export interface ColumnBrief {
  name: string
  dtype: string
  sample_values?: unknown[]
}

export interface DatasourceSummary {
  id: number
  name: string
  status: string
  row_count: number | null
  created_at: string | null
  /** 仅上传 / 创建响应里携带精简 columns */
  columns?: ColumnBrief[]
}

/** 数值列统计（详情接口的完整 schema 中才有） */
export interface NumericStats {
  min: number | null
  max: number | null
  mean?: number | null
  median?: number | null
  std?: number | null
  [key: string]: unknown
}

export interface ColumnSchema {
  name: string
  dtype: string
  sample_values?: unknown[]
  numeric_stats?: NumericStats
  datetime_stats?: Record<string, unknown>
  [key: string]: unknown
}

export interface DatasourceSchema {
  columns: ColumnSchema[]
  row_count?: number
  file_type?: string
  meta?: Record<string, unknown>
  [key: string]: unknown
}

export interface DatasourceDetail extends DatasourceSummary {
  file_path: string
  schema: DatasourceSchema
}

// ----------------------------- 执行结果 -----------------------------

export type CellValue = string | number | boolean | null

export interface TableResult {
  type?: 'table'
  columns: string[]
  data: CellValue[][]
  total_rows?: number
  returned_rows?: number
  truncated?: boolean
}

export interface SingleValueResult {
  type: 'single_value'
  value: CellValue
}

/** record 类型在后端是裸 dict（没有 type 包装） */
export type RecordResult = Record<string, unknown>

export type ResultData = TableResult | SingleValueResult | RecordResult | null

export function isTableResult(data: ResultData): data is TableResult {
  return (
    !!data &&
    Array.isArray((data as TableResult).columns) &&
    Array.isArray((data as TableResult).data)
  )
}

export function isSingleValueResult(
  data: ResultData,
): data is SingleValueResult {
  return !!data && (data as SingleValueResult).type === 'single_value'
}

// ----------------------------- 自动可视化 -----------------------------

export type ChartType = 'bar' | 'line' | 'scatter'

/** 后端 chart_advisor 给出的图表推荐（对应 {type,x,y}） */
export interface ChartSuggestion {
  type: ChartType
  x: string
  y: string
}

// --------------------------- 数字回溯判定 ---------------------------

export type NumberKind =
  | 'number'
  | 'percent'
  | 'multiplier'
  | 'currency'
  | 'count'
  | 'ordinal'
  | 'chinese_number'
  | string

/** 数字证据位置（对应后端 NumericBacktracker 的 location） */
export interface EvidenceLocation {
  /** 表格单元格证据没有 type 字段，只有 row/column */
  type?: 'single_value' | 'record' | 'table_meta'
  row?: number
  column?: string
  column_index?: number
  evidence_value?: number
  /** type=table_meta 时 */
  matched?: 'row_rank' | 'row_count'
  total_rows?: number
  rank?: number
  count?: number
  /** type=record 时 */
  key?: string
  /** 百分比 / 倍数按换算口径匹配时，实际命中的候选值 */
  matched_value?: number
}

export interface NumberVerdict {
  /** 在答案文本中的原始片段，如 "300"、"4个"、"第一"、"230%" */
  raw: string
  value: number
  kind: NumberKind
  /** 在答案文本中的字符偏移（Python/JS 对 BMP 文本一致） */
  position: number
  found: boolean
  evidence: EvidenceLocation | null
}

// ----------------------------- 分析步骤 -----------------------------

export type StepType = 'plan' | 'code' | 'execute' | 'verify' | 'answer'

export interface VerifyCheck {
  layer: 'syntax' | 'semantic' | 'consistency' | string
  status: 'pass' | 'fail' | string
  detail: string
}

export interface StepMeta {
  // plan
  result_format?: string
  duration_ms?: number
  /** 多轮追问时 plan 步骤记录的上一轮会话信息 */
  followup?: {
    prev_session_id: number
    prev_question: string
  }
  // code
  char_count?: number
  static_validation?: string
  // execute
  success?: boolean
  result_type?: string
  attempts?: number
  autofix_history?: Array<Record<string, unknown>>
  autofix_error?: string | null
  stdout_tail?: string
  result_data?: ResultData
  total_rows?: number
  returned_rows?: number
  /** execute 步骤上持久化的自动图表推荐 */
  chart_suggestion?: ChartSuggestion | null
  error?: string
  // verify
  stage?: string
  passed?: boolean
  checks?: VerifyCheck[]
  // answer
  initial_answer?: string
  /** 初稿每个数字的回溯判定（幻觉数字 found=false，供红色展示初稿） */
  initial_verdicts?: NumberVerdict[]
  regeneration_rounds?: number
  total_numbers?: number
  hallucination_count?: number
  hallucinations?: string[]
  number_verdicts?: NumberVerdict[]
  regeneration_error?: string | null
  regeneration_history?: Array<Record<string, unknown>>
  final_checks?: VerifyCheck[]
  [key: string]: unknown
}

export interface AnalysisStep {
  id: number
  step_type: StepType
  content: string
  meta: StepMeta | null
  created_at: string | null
}

// --------------------------- query / session ---------------------------

export interface QueryResponse {
  session_id: number
  question: string
  status: string
  plan: string
  answer: string
  verified: boolean
  hallucination_count: number
  result_data: ResultData
  /** 多轮追问时上一轮会话 id（首轮为 null） */
  prev_session_id?: number | null
  /** table 型结果的自动图表推荐 */
  chart_suggestion?: ChartSuggestion | null
  /** 答案中每个数字的回溯判定（数字级溯源） */
  answer_numbers: NumberVerdict[]
  steps: AnalysisStep[]
}

export interface SessionDetail {
  session_id: number
  datasource_id?: number
  question: string
  status: string
  created_at?: string | null
  plan?: string
  answer?: string | null
  verified: boolean
  hallucination_count: number
  result_data: ResultData
  prev_session_id?: number | null
  chart_suggestion?: ChartSuggestion | null
  answer_numbers?: NumberVerdict[]
  steps: AnalysisStep[]
}

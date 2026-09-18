/**
 * 自动可视化：依据后端 chart_suggestion 用 recharts 渲染柱状图 / 折线图 / 散点图。
 * 无法与结果表列对齐或没有可绘数值时不渲染。
 */
import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import {
  isTableResult,
  type ChartSuggestion,
  type ResultData,
} from '../api/types'

const MAX_BAR_CATEGORIES = 50
const MAX_POINTS = 500

const TYPE_META: Record<
  ChartSuggestion['type'],
  { label: string; max: number }
> = {
  bar: { label: '柱状图', max: MAX_BAR_CATEGORIES },
  line: { label: '折线图', max: MAX_POINTS },
  scatter: { label: '散点图', max: MAX_POINTS },
}

interface ChartPoint {
  /** X 轴展示文案（bar/line 的类目值） */
  label: string
  /** scatter 需要数值型 x；bar/line 用 label 即可 */
  x: number | string
  y: number
}

export default function ChartPanel({
  resultData,
  suggestion,
}: {
  resultData: ResultData
  suggestion: ChartSuggestion
}) {
  const table = isTableResult(resultData) ? resultData : null
  if (!table) return null

  const xIndex = table.columns.indexOf(suggestion.x)
  const yIndex = table.columns.indexOf(suggestion.y)
  if (xIndex < 0 || yIndex < 0) return null

  const points: ChartPoint[] = []
  for (const row of table.data) {
    const y = Number(row[yIndex])
    if (!Number.isFinite(y)) continue
    const rawX = row[xIndex]
    const label = rawX === null || rawX === undefined ? '' : String(rawX)
    points.push({
      label,
      x: suggestion.type === 'scatter' ? Number(rawX) : label,
      y,
    })
  }
  if (points.length === 0) return null

  const meta = TYPE_META[suggestion.type]
  const shown = points.slice(0, meta.max)
  const hiddenCount = points.length - shown.length

  const axisTick = { fontSize: 12, fill: '#64748b' }
  const grid = <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
  const tooltip = (
    <Tooltip
      contentStyle={{
        borderRadius: 8,
        border: '1px solid #e2e8f0',
        fontSize: 12,
      }}
    />
  )

  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <div className="mb-3 flex flex-wrap items-center gap-2 text-xs text-slate-500">
        <svg
          className="h-4 w-4 text-blue-500"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M3 3v18h18" />
          <path d="M7 15l4-4 3 3 5-6" />
        </svg>
        <span className="font-medium text-slate-600">自动可视化</span>
        <span className="rounded bg-blue-50 px-2 py-0.5 font-medium text-blue-700 ring-1 ring-inset ring-blue-100">
          {meta.label} · {suggestion.x} × {suggestion.y}
        </span>
        {hiddenCount > 0 && (
          <span className="text-slate-400">
            （仅显示前 {shown.length} 项，共 {points.length} 项）
          </span>
        )}
      </div>

      <div className="h-72 w-full">
        <ResponsiveContainer width="100%" height="100%">
          {suggestion.type === 'bar' ? (
            <BarChart data={shown} margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
              {grid}
              <XAxis dataKey="label" tick={axisTick} />
              <YAxis tick={axisTick} width={48} />
              {tooltip}
              <Bar dataKey="y" name={suggestion.y} fill="#10b981" radius={[4, 4, 0, 0]} />
            </BarChart>
          ) : suggestion.type === 'line' ? (
            <LineChart data={shown} margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
              {grid}
              <XAxis dataKey="label" tick={axisTick} />
              <YAxis tick={axisTick} width={48} />
              {tooltip}
              <Line
                type="monotone"
                dataKey="y"
                name={suggestion.y}
                stroke="#2563eb"
                strokeWidth={2}
                dot={{ r: 3 }}
              />
            </LineChart>
          ) : (
            <ScatterChart margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
              {grid}
              <XAxis
                type="number"
                dataKey="x"
                name={suggestion.x}
                tick={axisTick}
              />
              <YAxis
                type="number"
                dataKey="y"
                name={suggestion.y}
                tick={axisTick}
                width={48}
              />
              {tooltip}
              <Scatter data={shown} fill="#6366f1" />
            </ScatterChart>
          )}
        </ResponsiveContainer>
      </div>
    </div>
  )
}

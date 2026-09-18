/**
 * Schema 卡片：数据源名称 / 行数 / 列数 + 每个列的名称与 dtype 徽章。
 * 数值列额外展示 min–max 区间（来自详情接口的 numeric_stats）。
 */
import type { ColumnSchema } from '../api/types'
import { formatNumber } from '../lib/evidence'
import { useChatStore } from '../store/useChatStore'

const DTYPE_STYLE: Record<string, { label: string; className: string }> = {
  numeric: {
    label: '数值',
    className: 'bg-blue-50 text-blue-700 ring-blue-200',
  },
  categorical: {
    label: '类别',
    className: 'bg-violet-50 text-violet-700 ring-violet-200',
  },
  datetime: {
    label: '日期',
    className: 'bg-amber-50 text-amber-700 ring-amber-200',
  },
  empty: {
    label: '空列',
    className: 'bg-slate-100 text-slate-500 ring-slate-200',
  },
}

function dtypeBadge(dtype: string) {
  return (
    DTYPE_STYLE[dtype] ?? {
      label: dtype,
      className: 'bg-slate-100 text-slate-600 ring-slate-200',
    }
  )
}

function ColumnRow({ col }: { col: ColumnSchema }) {
  const badge = dtypeBadge(col.dtype)
  const stats = col.numeric_stats
  const samples = (col.sample_values ?? [])
    .map((v) => (v === null ? 'null' : String(v)))
    .join('、')
  const range =
    stats && stats.min !== null && stats.max !== null
      ? `${formatNumber(Number(stats.min))} ~ ${formatNumber(Number(stats.max))}`
      : null

  return (
    <li
      title={samples ? `示例值：${samples}` : undefined}
      className="flex items-center gap-2 rounded-md px-2 py-1.5 hover:bg-slate-50"
    >
      <span className="truncate font-mono text-xs text-slate-700">
        {col.name}
      </span>
      <span
        className={`ml-auto shrink-0 rounded-full px-2 py-0.5 text-[10px] font-medium ring-1 ring-inset ${badge.className}`}
      >
        {badge.label}
      </span>
      {range && (
        <span className="hidden w-28 shrink-0 text-right text-[10px] tabular-nums text-slate-400 xl:block">
          {range}
        </span>
      )}
    </li>
  )
}

export default function SchemaCard() {
  const datasource = useChatStore((s) => s.activeDatasource)
  if (!datasource) return null

  const columns = datasource.schema?.columns ?? []
  const fileType = String(
    datasource.schema?.file_type ?? datasource.name.split('.').pop() ?? '',
  ).toUpperCase()

  return (
    <div className="rounded-xl border border-slate-200 bg-white p-3.5 shadow-sm">
      <div className="flex items-start gap-2">
        <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-indigo-50 text-indigo-600">
          <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <ellipse cx="12" cy="5" rx="9" ry="3" />
            <path d="M3 5v6c0 1.66 4 3 9 3s9-1.34 9-3V5" />
            <path d="M3 11v6c0 1.66 4 3 9 3s9-1.34 9-3v-6" />
          </svg>
        </div>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-semibold text-slate-800" title={datasource.name}>
            {datasource.name}
          </p>
          <p className="mt-0.5 flex items-center gap-1 text-[11px] text-slate-400">
            <span className="inline-block h-1.5 w-1.5 rounded-full bg-emerald-500" />
            已就绪 · {fileType}
          </p>
        </div>
      </div>

      <div className="mt-3 grid grid-cols-2 gap-2">
        <div className="rounded-lg bg-slate-50 px-3 py-2">
          <p className="text-lg font-semibold tabular-nums text-slate-800">
            {datasource.row_count ?? '-'}
          </p>
          <p className="text-[11px] text-slate-500">行数</p>
        </div>
        <div className="rounded-lg bg-slate-50 px-3 py-2">
          <p className="text-lg font-semibold tabular-nums text-slate-800">
            {columns.length}
          </p>
          <p className="text-[11px] text-slate-500">列数</p>
        </div>
      </div>

      <ul className="thin-scroll mt-2 max-h-64 divide-y divide-slate-100 overflow-auto pr-1">
        {columns.map((col) => (
          <ColumnRow key={col.name} col={col} />
        ))}
      </ul>
    </div>
  )
}

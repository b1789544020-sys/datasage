/**
 * C. 溯源面板：点击答案中的数字后从右侧滑出。
 * 顶部给出数字判定与证据说明，下方展示 result_data 原始数据行，
 * 命中证据的行 / 单元格高亮。
 */
import type { CellValue, NumberVerdict, ResultData } from '../api/types'
import { isSingleValueResult, isTableResult } from '../api/types'
import { evidenceSummary, formatNumber, kindLabel } from '../lib/evidence'
import { useChatStore } from '../store/useChatStore'
import DataTable from './DataTable'

function MetaRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-2 text-xs">
      <span className="w-16 shrink-0 text-slate-400">{label}</span>
      <span className="flex-1 font-medium text-slate-700">{value}</span>
    </div>
  )
}

function EvidenceDetails({ verdict }: { verdict: NumberVerdict }) {
  const loc = verdict.evidence
  if (!verdict.found || !loc) {
    return (
      <div className="space-y-1.5 rounded-lg border border-red-200 bg-red-50 p-3">
        <p className="text-xs leading-relaxed text-red-700">
          {evidenceSummary(verdict)}
        </p>
        <p className="text-[11px] leading-relaxed text-red-500/80">
          后端已尝试带真实数据重新生成答案；若最终一轮仍保留该数字，
          说明模型坚持使用了结果中不存在的数值。
        </p>
      </div>
    )
  }

  return (
    <div className="space-y-1.5 rounded-lg border border-emerald-200 bg-emerald-50 p-3">
      <p className="text-xs leading-relaxed text-emerald-800">
        {evidenceSummary(verdict)}
      </p>
      <div className="space-y-1 pt-1">
        {typeof loc.row === 'number' && (
          <MetaRow label="行位置" value={`第 ${loc.row + 1} 行（0-based ${loc.row}）`} />
        )}
        {loc.column !== undefined && (
          <MetaRow label="列" value={loc.column} />
        )}
        {loc.evidence_value !== undefined && (
          <MetaRow label="证据值" value={formatNumber(loc.evidence_value)} />
        )}
        {loc.type === 'table_meta' && loc.matched === 'row_rank' && (
          <MetaRow label="口径" value={`结果表 ${loc.total_rows} 行内存在第 ${loc.rank} 名`} />
        )}
        {loc.type === 'table_meta' && loc.matched === 'row_count' && (
          <MetaRow label="口径" value={`计数与结果表行数 ${loc.total_rows} 一致`} />
        )}
        {loc.type === 'record' && <MetaRow label="字段" value={loc.key ?? ''} />}
        {loc.matched_value !== undefined &&
          Math.abs(loc.matched_value - verdict.value) > 1e-9 && (
            <MetaRow
              label="匹配口径"
              value={`按 ${formatNumber(loc.matched_value)} 换算后命中`}
            />
          )}
      </div>
    </div>
  )
}


function ScalarView({ value }: { value: CellValue }) {
  return (
    <div className="rounded-lg bg-slate-50 px-4 py-6 text-center">
      <p className="text-xs text-slate-400">标量结果</p>
      <p className="mt-1 text-3xl font-bold tabular-nums text-slate-800">
        {value === null ? 'null' : String(value)}
      </p>
    </div>
  )
}

function RecordView({ data }: { data: Record<string, unknown> }) {
  return (
    <div className="overflow-hidden rounded-lg border border-slate-200">
      <table className="w-full text-left text-xs">
        <tbody>
          {Object.entries(data).map(([key, val]) => (
            <tr key={key} className="border-b border-slate-100 last:border-0">
              <td className="bg-slate-50 px-3 py-2 font-mono font-medium text-slate-600">
                {key}
              </td>
              <td className="px-3 py-2 tabular-nums text-slate-800">
                {typeof val === 'object'
                  ? JSON.stringify(val)
                  : String(val ?? 'null')}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function OriginalData({
  data,
  verdict,
}: {
  data: ResultData
  verdict: NumberVerdict
}) {
  const loc = verdict.evidence
  const highlightRow =
    loc?.type === 'table_meta' && loc.matched === 'row_rank'
      ? Math.max((loc.rank ?? 1) - 1, 0)
      : typeof loc?.row === 'number'
        ? loc.row
        : undefined

  return (
    <div>
      <div className="mb-2 flex items-center justify-between">
        <p className="text-xs font-semibold text-slate-700">原始结果数据</p>
        {isTableResult(data) && data.total_rows != null && (
          <span className="text-[11px] text-slate-400">
            共 {data.total_rows} 行
            {data.truncated &&
              ` · 展示前 ${data.returned_rows ?? data.data.length} 行`}
          </span>
        )}
      </div>
      {isTableResult(data) ? (
        <DataTable
          columns={data.columns}
          data={data.data}
          highlightRow={highlightRow}
          highlightColumn={loc?.column}
          maxHeightClass="max-h-[46vh]"
        />
      ) : isSingleValueResult(data) ? (
        <ScalarView value={data.value} />
      ) : data ? (
        <RecordView data={data as Record<string, unknown>} />
      ) : (
        <p className="text-xs text-slate-400">无结构化结果数据</p>
      )}
    </div>
  )
}

export default function EvidencePanel() {
  const evidence = useChatStore((s) => s.evidence)
  const closeEvidence = useChatStore((s) => s.closeEvidence)
  if (!evidence) return null

  const { verdict, resultData, question } = evidence

  return (
    <>
      {/* 小屏遮罩 */}
      <div
        className="fixed inset-0 z-30 bg-slate-900/30 sm:hidden"
        onClick={closeEvidence}
      />
      <aside className="thin-scroll fixed inset-y-0 right-0 z-40 flex w-full animate-drawer-in flex-col border-l border-slate-200 bg-white shadow-2xl sm:w-[440px]">
        <header className="flex items-center gap-2 border-b border-slate-200 px-4 py-3.5">
          <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-emerald-50 text-emerald-600">
            <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="11" cy="11" r="8" />
              <line x1="21" y1="21" x2="16.65" y2="16.65" />
            </svg>
          </span>
          <div className="min-w-0 flex-1">
            <h3 className="text-sm font-semibold text-slate-800">数据证据</h3>
            <p className="truncate text-[11px] text-slate-400">{question}</p>
          </div>
          <button
            type="button"
            onClick={closeEvidence}
            className="flex h-8 w-8 items-center justify-center rounded-lg text-slate-400 transition hover:bg-slate-100 hover:text-slate-600"
            aria-label="关闭证据面板"
          >
            <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </header>

        <div className="thin-scroll flex-1 space-y-4 overflow-auto px-4 py-4">
          {/* 被点击的数字 */}
          <div className="flex items-center gap-3 rounded-xl border border-slate-200 bg-slate-50 p-3">
            <span
              className={`rounded-md px-2.5 py-1 font-mono text-xl font-bold tabular-nums ${
                verdict.found
                  ? 'bg-white text-slate-800 ring-1 ring-emerald-300'
                  : 'bg-red-100 text-red-700 line-through'
              }`}
            >
              {verdict.raw}
            </span>
            <div className="space-y-0.5 text-xs text-slate-500">
              <p>
                类型：
                <span className="font-medium text-slate-700">
                  {kindLabel(verdict.kind)}
                </span>
              </p>
              <p>
                数值：
                <span className="font-medium tabular-nums text-slate-700">
                  {formatNumber(verdict.value)}
                </span>
              </p>
            </div>
            <span
              className={`ml-auto rounded-full px-2.5 py-1 text-[11px] font-medium ${
                verdict.found
                  ? 'bg-emerald-50 text-emerald-700 ring-1 ring-inset ring-emerald-200'
                  : 'bg-red-50 text-red-700 ring-1 ring-inset ring-red-200'
              }`}
            >
              {verdict.found ? '✓ 已回溯' : '⚠ 幻觉数字'}
            </span>
          </div>

          <EvidenceDetails verdict={verdict} />
          <OriginalData data={resultData} verdict={verdict} />
        </div>
      </aside>
    </>
  )
}

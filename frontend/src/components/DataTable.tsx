/**
 * 通用结果表：执行结果预览 / 证据抽屉原始数据行高亮都用它。
 */
import type { CellValue } from '../api/types'

interface DataTableProps {
  columns: string[]
  data: CellValue[][]
  /** 需要高亮的行下标（0-based，对应后端 result_data.data 的行号） */
  highlightRow?: number
  /** 需要高亮的列名（与 highlightRow 同时给出时，对应单元格加深高亮） */
  highlightColumn?: string
  maxRows?: number
  maxHeightClass?: string
  size?: 'sm' | 'md'
}

function Cell({ value }: { value: CellValue }) {
  if (value === null || value === undefined) {
    return <span className="text-slate-300">null</span>
  }
  if (typeof value === 'number') {
    return <span className="tabular-nums">{value}</span>
  }
  return <span>{String(value)}</span>
}

export default function DataTable({
  columns,
  data,
  highlightRow,
  highlightColumn,
  maxRows,
  maxHeightClass = 'max-h-80',
  size = 'sm',
}: DataTableProps) {
  const rows = maxRows ? data.slice(0, maxRows) : data
  const cellPad = size === 'sm' ? 'px-2.5 py-1.5' : 'px-3 py-2'

  return (
    <div
      className={`thin-scroll overflow-auto rounded-lg border border-slate-200 ${maxHeightClass}`}
    >
      <table className="w-full border-collapse text-left text-[13px]">
        <thead className="sticky top-0 z-10">
          <tr className="bg-slate-50">
            <th
              className={`${cellPad} w-10 border-b border-slate-200 text-xs font-medium text-slate-400`}
            >
              #
            </th>
            {columns.map((col) => (
              <th
                key={col}
                className={`${cellPad} whitespace-nowrap border-b border-slate-200 text-xs font-semibold text-slate-600`}
              >
                {col}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, rowIdx) => {
            const highlighted = highlightRow === rowIdx
            return (
              <tr
                key={rowIdx}
                className={
                  highlighted ? 'bg-emerald-50' : 'even:bg-slate-50/40'
                }
              >
                <td
                  className={`${cellPad} text-xs tabular-nums text-slate-400`}
                >
                  {rowIdx + 1}
                </td>
                {columns.map((col, colIdx) => {
                  const isEvidenceCell =
                    highlighted && highlightColumn === col
                  return (
                    <td
                      key={col}
                      className={`${cellPad} whitespace-nowrap text-slate-700 ${
                        isEvidenceCell
                          ? 'rounded bg-emerald-200/70 font-semibold text-emerald-900 ring-1 ring-inset ring-emerald-400'
                          : ''
                      }`}
                    >
                      <Cell value={row[colIdx] ?? null} />
                    </td>
                  )
                })}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

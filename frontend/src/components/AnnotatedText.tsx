/**
 * 答案文本的数字级溯源渲染：
 * - 回溯通过的数字：正文颜色 + 浅绿色下划线，hover 显示证据一句话摘要；
 * - 幻觉数字：红色高亮 + 删除线（正常流程会被重生成清掉，若最后一轮仍有则显示）；
 * - 点击任一数字 → 打开证据抽屉（EvidencePanel）。
 *
 * 数字区间取后端 verdict 的 position/raw；若切片对不上（极端字符情况），
 * 退化为从当前游标向后 indexOf，再不行就跳过该标注。
 */
import { Fragment } from 'react'
import type { ReactNode } from 'react'

import type { NumberVerdict, ResultData } from '../api/types'
import { evidenceSummary, kindLabel } from '../lib/evidence'
import { useChatStore } from '../store/useChatStore'

interface AnnotatedTextProps {
  text: string
  verdicts: NumberVerdict[]
  resultData: ResultData
  question: string
  /** false 时数字不可点击（如只做红/绿展示的被丢弃初稿），默认 true */
  interactive?: boolean
}

export default function AnnotatedText({
  text,
  verdicts,
  resultData,
  question,
  interactive = true,
}: AnnotatedTextProps) {
  const openEvidence = useChatStore((s) => s.openEvidence)
  const sorted = [...verdicts].sort((a, b) => a.position - b.position)

  const segments: ReactNode[] = []
  let cursor = 0

  sorted.forEach((verdict, idx) => {
    let start = verdict.position
    let end = start + verdict.raw.length
    if (text.slice(start, end) !== verdict.raw) {
      const fallback = text.indexOf(verdict.raw, cursor)
      if (fallback < 0) return
      start = fallback
      end = start + verdict.raw.length
    }
    if (start < cursor) return

    if (start > cursor) {
      segments.push(
        <Fragment key={`text-${idx}`}>{text.slice(cursor, start)}</Fragment>,
      )
    }

    const summary = evidenceSummary(verdict)
    const numberClass = verdict.found
      ? 'text-slate-800 underline decoration-emerald-400 decoration-2 underline-offset-4'
      : 'bg-red-100 px-1 font-medium text-red-700 line-through decoration-red-500'
    segments.push(
      <span key={`num-${idx}`} className="group relative inline">
        {interactive ? (
          <button
            type="button"
            onClick={() => openEvidence(verdict, resultData, question)}
            className={`rounded-sm px-0.5 transition hover:bg-emerald-50 ${numberClass}`}
          >
            {text.slice(start, end)}
          </button>
        ) : (
          <span className={`rounded-sm px-0.5 ${numberClass}`}>
            {text.slice(start, end)}
          </span>
        )}
        <span className="pointer-events-none absolute bottom-full left-1/2 z-30 mb-1.5 hidden w-60 -translate-x-1/2 group-hover:block">
          <span className="block rounded-lg bg-slate-900 px-3 py-2 text-[11px] font-normal leading-relaxed text-white shadow-xl">
            <span
              className={`mb-1 block font-semibold ${
                verdict.found ? 'text-emerald-300' : 'text-red-300'
              }`}
            >
              {kindLabel(verdict.kind)} ·{' '}
              {verdict.found ? '已回溯' : '无法回溯'}
            </span>
            {summary}
            <span className="mt-1 block text-slate-400">点击查看数据证据</span>
          </span>
        </span>
      </span>
    )
    cursor = end
  })

  if (cursor < text.length) {
    segments.push(<Fragment key="text-tail">{text.slice(cursor)}</Fragment>)
  }

  return (
    <p className="text-[15px] leading-8 text-slate-800">
      {segments.length > 0 ? segments : text}
    </p>
  )
}

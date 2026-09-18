/**
 * 最终答案区：顶部反幻觉校验徽章 + 带数字溯源标注的正文 +
 * 自动可视化图表 + 导出 Markdown 报告按钮。
 */
import { useMemo, useState } from 'react'

import api from '../api/client'
import type { QueryResponse } from '../api/types'
import { downloadTextFile } from '../lib/download'
import AnnotatedText from './AnnotatedText'
import ChartPanel from './ChartPanel'

export default function AnswerPanel({ query }: { query: QueryResponse }) {
  const { verified, hallucination_count: count } = query
  const verdicts = query.answer_numbers ?? []
  const verifiedCount = verdicts.filter((v) => v.found).length
  const [exporting, setExporting] = useState(false)

  // 反幻觉演示核心素材：被数字回溯拦截、自动重写掉的第一版答案
  const answerMeta = useMemo(
    () => query.steps.find((s) => s.step_type === 'answer')?.meta ?? null,
    [query.steps],
  )
  const rounds = answerMeta?.regeneration_rounds ?? 0
  const initialAnswer = answerMeta?.initial_answer ?? ''
  const initialVerdicts = answerMeta?.initial_verdicts ?? []
  const initialFakeCount = initialVerdicts.filter((v) => !v.found).length
  // 默认展开：让"红色幻觉初稿 -> 绿色最终答案"的对比一眼可见
  const [showDraft, setShowDraft] = useState(true)

  async function handleExport() {
    setExporting(true)
    try {
      const markdown = await api.getQueryReport(query.session_id)
      downloadTextFile(
        `datasage-report-${query.session_id}.md`,
        markdown,
        'text/markdown;charset=utf-8',
      )
    } catch (error) {
      window.alert(
        `导出报告失败：${error instanceof Error ? error.message : String(error)}`,
      )
    } finally {
      setExporting(false)
    }
  }

  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        {verified && count === 0 ? (
          <span className="inline-flex items-center gap-1.5 rounded-full bg-emerald-50 px-3 py-1 text-xs font-medium text-emerald-700 ring-1 ring-inset ring-emerald-200">
            <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
              <polyline points="9 12 11 14 15 10" />
            </svg>
            已通过反幻觉校验
          </span>
        ) : (
          <span className="inline-flex items-center gap-1.5 rounded-full bg-red-50 px-3 py-1 text-xs font-medium text-red-700 ring-1 ring-inset ring-red-200">
            <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
              <line x1="12" y1="9" x2="12" y2="13" />
              <line x1="12" y1="17" x2="12.01" y2="17" />
            </svg>
            {count > 0 ? `有 ${count} 处数字无法回溯` : '结果校验未完全通过'}
          </span>
        )}

        <span className="text-[11px] text-slate-400">
          数字回溯 {verifiedCount}/{verdicts.length}
        </span>
        <button
          type="button"
          onClick={() => void handleExport()}
          disabled={exporting}
          className="ml-auto inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-2.5 py-1 text-xs font-medium text-slate-600 shadow-sm transition hover:border-blue-300 hover:bg-blue-50/50 hover:text-blue-700 disabled:cursor-not-allowed disabled:opacity-60"
          title="导出含问题、计划、图表、答案与数字证据的 Markdown 报告"
        >
          <svg
            className="h-3.5 w-3.5"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
            <polyline points="7 10 12 15 17 10" />
            <line x1="12" y1="15" x2="12" y2="3" />
          </svg>
          {exporting ? '导出中…' : '导出报告'}
        </button>
        <span className="text-[11px] tabular-nums text-slate-300">
          #{query.session_id}
        </span>
      </div>

      {rounds > 0 && initialAnswer && initialFakeCount > 0 && (
        <div className="mb-4 overflow-hidden rounded-xl border border-red-200 bg-red-50/60">
          <button
            type="button"
            onClick={() => setShowDraft((v) => !v)}
            className="flex w-full items-center gap-2 px-3 py-2 text-left"
          >
            <svg className="h-4 w-4 shrink-0 text-red-500" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
              <line x1="12" y1="9" x2="12" y2="13" />
              <line x1="12" y1="17" x2="12.01" y2="17" />
            </svg>
            <span className="text-xs font-semibold text-red-700">
              第 1 版答案检出 {initialFakeCount} 个无法溯源的幻觉数字，已被数字回溯拦截并自动重写
            </span>
            <svg
              className={`ml-auto h-3.5 w-3.5 shrink-0 text-red-400 transition-transform ${
                showDraft ? 'rotate-180' : ''
              }`}
              viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"
            >
              <polyline points="6 9 12 15 18 9" />
            </svg>
          </button>
          {showDraft && (
            <div className="border-t border-red-100 bg-white/70 px-3 py-2.5">
              <p className="mb-1 text-[11px] font-medium text-red-400">
                LLM 初稿（红色删除线 = 在真实执行结果中找不到证据的数字）
              </p>
              <AnnotatedText
                text={initialAnswer}
                verdicts={initialVerdicts}
                resultData={query.result_data}
                question={query.question}
                interactive={false}
              />
            </div>
          )}
        </div>
      )}

      <AnnotatedText
        text={query.answer}
        verdicts={verdicts}
        resultData={query.result_data}
        question={query.question}
      />

      {query.chart_suggestion && (
        <div className="mt-4">
          <ChartPanel
            resultData={query.result_data}
            suggestion={query.chart_suggestion}
          />
        </div>
      )}

      {verdicts.length > 0 && (
        <p className="mt-3 border-t border-dashed border-slate-100 pt-2 text-[11px] text-slate-400">
          <span className="underline decoration-emerald-400 decoration-2 underline-offset-2">
            带下划线的数字
          </span>
          均可回溯，悬停查看摘要、点击查看原始数据证据；
          <span className="text-red-500 line-through">红色删除线</span>
          表示无法回溯的幻觉数字。
        </p>
      )}
    </div>
  )
}

/**
 * 分析过程折叠步骤卡：plan / code / execute / verify / answer。
 * 左侧类型图标 + 标题 + 概要，点击展开详细内容；code 用深色等宽代码块。
 */
import { useState } from 'react'

import type { AnalysisStep, StepType, VerifyCheck } from '../api/types'
import { isTableResult, isSingleValueResult } from '../api/types'
import DataTable from './DataTable'

// ------------------------------ 图标 ------------------------------

function StepIcon({ type }: { type: StepType }) {
  const common = {
    className: 'h-4 w-4',
    viewBox: '0 0 24 24',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 2,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
  }
  switch (type) {
    case 'plan':
      return (
        <svg {...common}>
          <path d="M9 11l3 3L22 4" />
          <path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" />
        </svg>
      )
    case 'code':
      return (
        <svg {...common}>
          <polyline points="16 18 22 12 16 6" />
          <polyline points="8 6 2 12 8 18" />
        </svg>
      )
    case 'execute':
      return (
        <svg {...common}>
          <polygon points="5 3 19 12 5 21 5 3" />
        </svg>
      )
    case 'verify':
      return (
        <svg {...common}>
          <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
          <polyline points="9 12 11 14 15 10" />
        </svg>
      )
    case 'answer':
      return (
        <svg {...common}>
          <path d="M12 2l2.4 7.4H22l-6 4.6 2.3 7.4-6.3-4.6L5.7 21.4 8 14 2 9.4h7.6z" />
        </svg>
      )
  }
}

const STEP_STYLE: Record<StepType, { title: string; badge: string }> = {
  plan: { title: '分析计划', badge: 'bg-indigo-50 text-indigo-600' },
  code: { title: '生成代码', badge: 'bg-violet-50 text-violet-600' },
  execute: { title: '沙箱执行', badge: 'bg-sky-50 text-sky-600' },
  verify: { title: '结果验证', badge: 'bg-emerald-50 text-emerald-600' },
  answer: { title: '答案生成', badge: 'bg-amber-50 text-amber-600' },
}

// ------------------------------ 概要 ------------------------------

function stepSummary(step: AnalysisStep): string {
  const meta = step.meta ?? {}
  switch (step.step_type) {
    case 'plan':
      return [
        meta.result_format ? `输出：${meta.result_format}` : '',
        meta.duration_ms != null ? `${meta.duration_ms}ms` : '',
      ]
        .filter(Boolean)
        .join(' · ')
    case 'code':
      return `${meta.char_count ?? step.content.length} 字符 · 静态校验${
        meta.static_validation === 'passed' ? '通过' : '未通过'
      }`
    case 'execute': {
      if (meta.success === false) return '执行失败'
      const parts = [
        meta.attempts != null ? `第 ${meta.attempts} 次尝试成功` : '执行成功',
        meta.result_type ? `类型 ${meta.result_type}` : '',
        meta.total_rows != null ? `共 ${meta.total_rows} 行` : '',
        meta.duration_ms != null ? `${meta.duration_ms}ms` : '',
      ]
      return parts.filter(Boolean).join(' · ')
    }
    case 'verify': {
      const checks = meta.checks ?? []
      const passed = checks.filter((c) => c.status === 'pass').length
      return meta.passed
        ? `${passed}/${checks.length || 3} 项检查通过`
        : `${checks.length - passed} 项检查未通过`
    }
    case 'answer':
      return [
        meta.total_numbers != null ? `数字 ${meta.total_numbers} 个` : '',
        `幻觉 ${meta.hallucination_count ?? 0} 处`,
        meta.regeneration_rounds
          ? `重生成 ${meta.regeneration_rounds} 轮`
          : '',
      ]
        .filter(Boolean)
        .join(' · ')
  }
}

function stepSucceeded(step: AnalysisStep): boolean | null {
  const meta = step.meta
  if (step.step_type === 'execute') return meta?.success === true
  if (step.step_type === 'verify') return meta?.passed !== false
  return null
}

// ------------------------------ 主组件 ------------------------------

const DEFAULT_OPEN: Record<StepType, boolean> = {
  plan: true,
  code: true,
  execute: true,
  verify: false,
  answer: false,
}

export default function StepCard({ step }: { step: AnalysisStep }) {
  const [open, setOpen] = useState(DEFAULT_OPEN[step.step_type])
  const config = STEP_STYLE[step.step_type]
  const succeeded = stepSucceeded(step)

  return (
    <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-3 px-3.5 py-3 text-left transition hover:bg-slate-50"
      >
        <span
          className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg ${config.badge}`}
        >
          <StepIcon type={step.step_type} />
        </span>
        <span className="shrink-0 text-sm font-semibold text-slate-800">
          {config.title}
        </span>
        <span className="hidden min-w-0 flex-1 truncate text-xs text-slate-400 sm:block">
          {stepSummary(step)}
        </span>
        {succeeded !== null && (
          <span
            className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-medium ${
              succeeded
                ? 'bg-emerald-50 text-emerald-700'
                : 'bg-red-50 text-red-700'
            }`}
          >
            {succeeded ? '成功' : '失败'}
          </span>
        )}
        <svg
          className={`h-4 w-4 shrink-0 text-slate-400 transition-transform ${
            open ? 'rotate-180' : ''
          }`}
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <polyline points="6 9 12 15 18 9" />
        </svg>
      </button>

      {open && (
        <div className="border-t border-slate-100 px-3.5 py-3">
          <StepBody step={step} />
        </div>
      )}
    </div>
  )
}


// ------------------------------ 展开内容 ------------------------------

function StepBody({ step }: { step: AnalysisStep }) {
  switch (step.step_type) {
    case 'plan':
      return (
        <p className="whitespace-pre-wrap text-sm leading-relaxed text-slate-600">
          {step.content}
        </p>
      )
    case 'code':
      return (
        <div className="thin-scroll max-h-96 overflow-auto rounded-lg bg-slate-900 px-4 py-3">
          <pre className="whitespace-pre font-mono text-[12.5px] leading-relaxed text-slate-100">
            {step.content}
          </pre>
        </div>
      )
    case 'execute':
      return <ExecuteBody step={step} />
    case 'verify':
      return <VerifyBody step={step} />
    case 'answer':
      return <AnswerBody step={step} />
  }
}

function ExecuteBody({ step }: { step: AnalysisStep }) {
  const meta = step.meta ?? {}
  const result = meta.result_data ?? null
  const attempts = Array.isArray(meta.autofix_history)
    ? meta.autofix_history
    : []

  return (
    <div className="space-y-3">
      {meta.success === false && (
        <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
          {meta.error ?? step.content}
        </div>
      )}

      {isTableResult(result) && (
        <div>
          <p className="mb-1.5 text-xs font-medium text-slate-500">
            执行结果
            {result.total_rows != null && (
              <span className="ml-1 text-slate-400">
                （共 {result.total_rows} 行
                {result.truncated &&
                  `，展示前 ${result.returned_rows ?? result.data.length} 行`}
                ）
              </span>
            )}
          </p>
          <DataTable
            columns={result.columns}
            data={result.data}
            maxRows={5}
            maxHeightClass="max-h-64"
          />
        </div>
      )}

      {isSingleValueResult(result) && (
        <div className="rounded-lg bg-slate-50 px-4 py-3">
          <p className="text-xs text-slate-400">标量结果</p>
          <p className="mt-0.5 text-xl font-semibold tabular-nums text-slate-800">
            {String(result.value)}
          </p>
        </div>
      )}

      {result && !isTableResult(result) && !isSingleValueResult(result) && (
        <div>
          <p className="mb-1.5 text-xs font-medium text-slate-500">执行结果</p>
          <pre className="thin-scroll max-h-60 overflow-auto rounded-lg bg-slate-900 px-4 py-3 font-mono text-[12px] text-slate-100">
            {JSON.stringify(result, null, 2)}
          </pre>
        </div>
      )}

      {attempts.length > 0 && (
        <details className="text-xs">
          <summary className="cursor-pointer font-medium text-amber-600">
            自动修复记录（{attempts.length} 次失败尝试）
          </summary>
          <ul className="mt-2 space-y-2">
            {attempts.map((item, idx) => (
              <li
                key={idx}
                className="rounded-lg border border-amber-100 bg-amber-50/60 px-3 py-2 text-slate-600"
              >
                <span className="font-medium text-amber-700">
                  第 {idx + 1} 次失败：
                </span>
                {String(item.error ?? '未知错误').slice(0, 300)}
              </li>
            ))}
          </ul>
        </details>
      )}

      {meta.stdout_tail && (
        <details className="text-xs">
          <summary className="cursor-pointer font-medium text-slate-500">
            沙箱标准输出
          </summary>
          <pre className="thin-scroll mt-2 max-h-40 overflow-auto rounded-lg bg-slate-900 px-3 py-2 font-mono text-[11.5px] text-slate-300">
            {meta.stdout_tail}
          </pre>
        </details>
      )}
    </div>
  )
}

const LAYER_LABELS: Record<string, string> = {
  syntax: '语法层',
  semantic: '语义层',
  consistency: '一致层',
}

function ChecksList({ checks }: { checks: VerifyCheck[] }) {
  return (
    <ul className="space-y-1.5">
      {checks.map((check, idx) => {
        const passed = check.status === 'pass'
        return (
          <li key={idx} className="flex items-start gap-2 text-xs">
            <span
              className={`mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full text-white ${
                passed ? 'bg-emerald-500' : 'bg-red-500'
              }`}
            >
              <svg className="h-2.5 w-2.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.5" strokeLinecap="round" strokeLinejoin="round">
                {passed ? (
                  <polyline points="20 6 9 17 4 12" />
                ) : (
                  <>
                    <line x1="18" y1="6" x2="6" y2="18" />
                    <line x1="6" y1="6" x2="18" y2="18" />
                  </>
                )}
              </svg>
            </span>
            <span className="shrink-0 rounded bg-slate-100 px-1.5 py-0.5 font-medium text-slate-500">
              {LAYER_LABELS[check.layer] ?? check.layer}
            </span>
            <span className="leading-relaxed text-slate-600">{check.detail}</span>
          </li>
        )
      })}
    </ul>
  )
}

function VerifyBody({ step }: { step: AnalysisStep }) {
  const checks = step.meta?.checks ?? []
  return (
    <div>
      <p className="mb-2 text-xs text-slate-500">
        三层验证：执行结构 syntax / 语义 sanity / 一致性（答案数字回溯）
      </p>
      <ChecksList checks={checks} />
    </div>
  )
}

function AnswerBody({ step }: { step: AnalysisStep }) {
  const meta = step.meta ?? {}
  const history = Array.isArray(meta.regeneration_history)
    ? meta.regeneration_history
    : []
  const finalChecks = Array.isArray(meta.final_checks) ? meta.final_checks : []

  return (
    <div className="space-y-3">
      {meta.initial_answer &&
        (meta.regeneration_rounds ?? 0) > 0 && (
          <div>
            <p className="mb-1 text-xs font-medium text-red-500">
              初稿（检出幻觉数字，已自动重生成）
            </p>
            <p className="rounded-lg border border-red-100 bg-red-50/60 px-3 py-2 text-xs leading-relaxed text-slate-500 line-through decoration-red-300">
              {meta.initial_answer}
            </p>
          </div>
        )}

      {history.length > 0 && (
        <div>
          <p className="mb-1 text-xs font-medium text-slate-500">
            反幻觉重生成记录（{history.length} 轮）
          </p>
          <ul className="space-y-1.5">
            {history.map((item, idx) => (
              <li
                key={idx}
                className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-600"
              >
                <span className="font-medium text-slate-700">
                  第 {Number(item.round ?? idx + 1)} 轮：
                </span>
                移除无法回溯的数字{' '}
                <span className="font-medium text-red-600">
                  {((item.removed_hallucinations as string[]) ?? []).join('、') || '—'}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {meta.regeneration_error && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700">
          重生成调用异常：{meta.regeneration_error}
        </div>
      )}

      {finalChecks.length > 0 && (
        <details className="text-xs" open>
          <summary className="cursor-pointer font-medium text-slate-500">
            最终一致性校验（含数字回溯）
          </summary>
          <div className="mt-2">
            <ChecksList checks={finalChecks as VerifyCheck[]} />
          </div>
        </details>
      )}

      <p className="text-[11px] leading-relaxed text-slate-400">
        最终答案渲染在下方答案区，答案中的每个数字都带回溯标记，可点击查看数据证据。
      </p>
    </div>
  )
}

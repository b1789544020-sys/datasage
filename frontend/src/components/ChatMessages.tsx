/**
 * B. 对话区：用户问题气泡 + 助手的折叠步骤卡与最终答案；等待中显示 loading。
 */
import { useEffect, useRef } from 'react'

import { useChatStore } from '../store/useChatStore'
import AnswerPanel from './AnswerPanel'
import StepCard from './StepCard'

const SUGGESTIONS = [
  '各地区销售额总和是多少？哪个地区最高？',
  '销量最好的品类是什么？',
  '每个地区分别有多少笔订单？',
]

const LOADING_STEPS = ['生成分析计划', '沙箱执行 pandas 代码', '三层结果验证', '撰写答案并做数字回溯']

function LoadingSteps() {
  return (
    <div className="rounded-xl border border-slate-200 bg-white px-4 py-4 shadow-sm">
      <div className="mb-3 flex items-center gap-2 text-sm font-medium text-slate-700">
        <svg className="h-4 w-4 animate-spin text-blue-600" viewBox="0 0 24 24" fill="none">
          <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
          <path className="opacity-90" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
        </svg>
        正在执行完整可信分析链路…
      </div>
      <ul className="space-y-2">
        {LOADING_STEPS.map((label, idx) => (
          <li
            key={label}
            className="flex items-center gap-2 text-xs text-slate-500"
            style={{ animation: `fade-in-up 0.5s ${idx * 0.15}s both` }}
          >
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-blue-400" />
            {label}
          </li>
        ))}
      </ul>
    </div>
  )
}

function EmptyState() {
  const ask = useChatStore((s) => s.ask)
  const hasDatasource = useChatStore((s) => s.activeDatasource !== null)

  return (
    <div className="mx-auto max-w-xl pt-10 text-center">
      <h2 className="text-2xl font-bold tracking-tight text-slate-800">
        用自然语言向数据提问
      </h2>
      <p className="mt-2 text-sm leading-relaxed text-slate-500">
        DataSage 会让大模型生成 pandas 代码，在沙箱中安全执行，
        并对答案做反幻觉校验与数字回溯——每个结论都可溯源。
      </p>
      <div className="mt-6 space-y-2">
        {SUGGESTIONS.map((q) => (
          <button
            key={q}
            type="button"
            disabled={!hasDatasource}
            onClick={() => void ask(q)}
            className="block w-full rounded-lg border border-slate-200 bg-white px-4 py-2.5 text-left text-sm text-slate-600 shadow-sm transition hover:border-blue-300 hover:bg-blue-50/40 hover:text-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {q}
          </button>
        ))}
      </div>
      {!hasDatasource && (
        <p className="mt-4 text-xs text-slate-400">
          请先在左侧上传 CSV / Excel，或一键加载示例数据
        </p>
      )}
    </div>
  )
}

export default function ChatMessages() {
  const messages = useChatStore((s) => s.messages)
  const querying = useChatStore((s) => s.querying)
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [messages.length, querying])

  if (messages.length === 0 && !querying) {
    return (
      <div className="thin-scroll flex-1 overflow-auto">
        <EmptyState />
      </div>
    )
  }

  return (
    <div className="thin-scroll flex-1 overflow-auto">
      <div className="mx-auto w-full max-w-3xl space-y-5 px-4 py-6">
        {messages.map((message) => {
          if (message.role === 'user') {
            return (
              <div key={message.id} className="flex justify-end animate-fade-in-up">
                <div className="max-w-[82%] rounded-2xl rounded-br-sm bg-blue-600 px-4 py-2.5 text-sm leading-relaxed text-white shadow-sm">
                  {message.question}
                </div>
              </div>
            )
          }

          if (message.error) {
            return (
              <div key={message.id} className="flex animate-fade-in-up">
                <div className="max-w-[82%] rounded-2xl rounded-bl-sm border border-red-200 bg-red-50 px-4 py-2.5 text-sm text-red-700">
                  分析失败：{message.error}
                </div>
              </div>
            )
          }

          const query = message.query
          if (!query) return null

          return (
            <div key={message.id} className="space-y-3 animate-fade-in-up">
              <p className="px-1 text-xs font-medium uppercase tracking-wide text-slate-400">
                分析过程 · 5 步
              </p>
              <div className="space-y-2.5">
                {query.steps.map((step) => (
                  <StepCard key={step.id} step={step} />
                ))}
              </div>
              <p className="px-1 pt-1 text-xs font-medium uppercase tracking-wide text-slate-400">
                最终答案
              </p>
              <AnswerPanel query={query} />
            </div>
          )
        })}

        {querying && <LoadingSteps />}
        <div ref={bottomRef} />
      </div>
    </div>
  )
}

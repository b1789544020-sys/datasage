/**
 * 底部提问输入框：Enter 发送 / Shift+Enter 换行，loading 或无数据源时禁用。
 */
import { useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'

import { useChatStore } from '../store/useChatStore'

export default function ChatInput() {
  const [value, setValue] = useState('')
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  const ask = useChatStore((s) => s.ask)
  const querying = useChatStore((s) => s.querying)
  const datasourceName = useChatStore((s) => s.activeDatasource?.name)

  const disabled = !datasourceName
  const canSend = !disabled && !querying && value.trim().length > 0

  const autoGrow = () => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 140)}px`
  }

  const submit = () => {
    if (!canSend) return
    void ask(value)
    setValue('')
    requestAnimationFrame(() => {
      if (textareaRef.current) textareaRef.current.style.height = 'auto'
    })
  }

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      submit()
    }
  }

  return (
    <div className="border-t border-slate-200 bg-white/80 px-4 py-3 backdrop-blur">
      <div className="mx-auto w-full max-w-3xl">
        <p className="mb-1.5 text-[11px] text-slate-400">
          {datasourceName
            ? `当前数据源：${datasourceName}`
            : '请先在左侧上传或选择数据源'}
        </p>
        <div
          className={`flex items-end gap-2 rounded-2xl border bg-white px-3 py-2 shadow-sm transition ${
            disabled
              ? 'border-slate-200 opacity-70'
              : 'border-slate-300 focus-within:border-blue-400 focus-within:ring-2 focus-within:ring-blue-100'
          }`}
        >
          <textarea
            ref={textareaRef}
            value={value}
            disabled={disabled}
            rows={1}
            placeholder={
              disabled ? '上传数据后即可提问…' : '例如：各地区销售额总和是多少？'
            }
            onChange={(e) => {
              setValue(e.target.value)
              autoGrow()
            }}
            onKeyDown={onKeyDown}
            className="thin-scroll max-h-[140px] flex-1 resize-none bg-transparent py-1 text-sm leading-relaxed text-slate-800 placeholder:text-slate-400 focus:outline-none disabled:cursor-not-allowed"
          />
          <button
            type="button"
            disabled={!canSend}
            onClick={submit}
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-blue-600 text-white transition hover:bg-blue-700 disabled:cursor-not-allowed disabled:bg-slate-300"
            aria-label="发送"
          >
            {querying ? (
              <svg className="h-4 w-4 animate-spin" viewBox="0 0 24 24" fill="none">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-90" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
              </svg>
            ) : (
              <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <line x1="22" y1="2" x2="11" y2="13" />
                <polygon points="22 2 15 22 11 13 2 9 22 2" />
              </svg>
            )}
          </button>
        </div>
        <p className="mt-1.5 text-center text-[11px] text-slate-300">
          Enter 发送 · Shift + Enter 换行 · 答案数字经反幻觉回溯，可点击溯源
        </p>
      </div>
    </div>
  )
}

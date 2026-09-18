/**
 * A. 数据上传区：拖拽 / 点击上传 CSV、Excel，一键加载内置示例，
 * 并列出历史数据源供切换。上传成功后由 SchemaCard 展示 schema。
 */
import { useRef, useState } from 'react'
import type { DragEvent } from 'react'

import { useChatStore } from '../store/useChatStore'
import SchemaCard from './SchemaCard'

const ACCEPT = '.csv,.xlsx,.xls'

export default function UploadPanel() {
  const fileInputRef = useRef<HTMLInputElement>(null)
  const [dragging, setDragging] = useState(false)

  const uploading = useChatStore((s) => s.uploading)
  const datasourceError = useChatStore((s) => s.datasourceError)
  const datasources = useChatStore((s) => s.datasources)
  const activeId = useChatStore((s) => s.activeDatasource?.id)
  const uploadFile = useChatStore((s) => s.uploadFile)
  const loadSample = useChatStore((s) => s.loadSample)
  const selectDatasource = useChatStore((s) => s.selectDatasource)

  const handleFiles = (files: FileList | null) => {
    const file = files?.[0]
    if (file) void uploadFile(file)
  }

  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault()
    setDragging(false)
    handleFiles(event.dataTransfer.files)
  }

  return (
    <div className="space-y-4">
      <div
        role="button"
        tabIndex={0}
        onClick={() => fileInputRef.current?.click()}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') fileInputRef.current?.click()
        }}
        onDragOver={(e) => {
          e.preventDefault()
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        className={`group cursor-pointer rounded-xl border-2 border-dashed px-4 py-6 text-center transition ${
          dragging
            ? 'border-blue-500 bg-blue-50'
            : 'border-slate-300 bg-slate-50/60 hover:border-blue-400 hover:bg-blue-50/40'
        }`}
      >
        <input
          ref={fileInputRef}
          type="file"
          accept={ACCEPT}
          className="hidden"
          onChange={(e) => {
            handleFiles(e.target.files)
            e.target.value = ''
          }}
        />
        <div className="mx-auto mb-2 flex h-10 w-10 items-center justify-center rounded-full bg-blue-100 text-blue-600 transition group-hover:bg-blue-600 group-hover:text-white">
          {uploading ? (
            <svg className="h-5 w-5 animate-spin" viewBox="0 0 24 24" fill="none">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
              <path className="opacity-90" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
            </svg>
          ) : (
            <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
              <polyline points="17 8 12 3 7 8" />
              <line x1="12" y1="3" x2="12" y2="15" />
            </svg>
          )}
        </div>
        <p className="text-sm font-medium text-slate-700">
          {uploading ? '正在解析数据…' : '拖拽文件到此处，或点击上传'}
        </p>
        <p className="mt-1 text-xs text-slate-400">支持 .csv / .xlsx / .xls</p>
      </div>


      <button
        type="button"
        disabled={uploading}
        onClick={() => void loadSample()}
        className="flex w-full items-center justify-center gap-2 rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm font-medium text-emerald-700 transition hover:bg-emerald-100 disabled:cursor-not-allowed disabled:opacity-60"
      >
        <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 2l2.4 7.4H22l-6 4.6 2.3 7.4-6.3-4.6L5.7 21.4 8 14 2 9.4h7.6z" />
        </svg>
        加载示例数据（sample_sales.csv）
      </button>

      {datasourceError && (
        <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs leading-relaxed text-red-700">
          {datasourceError}
        </div>
      )}

      <SchemaCard />

      {datasources.length > 0 && (
        <div>
          <p className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-slate-400">
            历史数据源
          </p>
          <ul className="space-y-1">
            {datasources.slice(0, 8).map((d) => (
              <li key={d.id}>
                <button
                  type="button"
                  onClick={() => void selectDatasource(d.id)}
                  className={`flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-xs transition ${
                    d.id === activeId
                      ? 'bg-blue-50 font-medium text-blue-700 ring-1 ring-inset ring-blue-200'
                      : 'text-slate-600 hover:bg-slate-100'
                  }`}
                >
                  <svg className="h-3.5 w-3.5 shrink-0 text-slate-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                    <polyline points="14 2 14 8 20 8" />
                  </svg>
                  <span className="truncate">{d.name}</span>
                  {d.row_count != null && (
                    <span className="ml-auto shrink-0 tabular-nums text-slate-400">
                      {d.row_count} 行
                    </span>
                  )}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

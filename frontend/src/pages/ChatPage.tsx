/**
 * DataSage 主界面：
 * - 左侧栏：数据上传区 + schema 卡片（小屏为抽屉）
 * - 中间：对话区（分析步骤卡 + 最终答案）+ 底部输入框
 * - 右侧：数字证据抽屉（EvidencePanel 自行挂载）
 */
import { useEffect, useState } from 'react'

import ChatInput from '../components/ChatInput'
import ChatMessages from '../components/ChatMessages'
import EvidencePanel from '../components/EvidencePanel'
import UploadPanel from '../components/UploadPanel'
import { useChatStore } from '../store/useChatStore'

function BrandMark() {
  return (
    <div className="flex items-center gap-2.5">
      <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-blue-600 to-violet-600 text-white shadow-sm">
        <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
          <polyline points="9 12 11 14 15 10" />
        </svg>
      </span>
      <div>
        <p className="text-base font-bold leading-tight text-slate-800">
          DataSage
        </p>
        <p className="text-[11px] leading-tight text-slate-400">
          可信数据分析引擎
        </p>
      </div>
    </div>
  )
}

export default function ChatPage() {
  const hydrate = useChatStore((s) => s.hydrate)
  const activeId = useChatStore((s) => s.activeDatasource?.id)
  const [sidebarOpen, setSidebarOpen] = useState(false)

  useEffect(() => {
    void hydrate()
  }, [hydrate])

  // 移动端：选中 / 上传数据源后自动收起侧栏
  useEffect(() => {
    if (activeId != null) setSidebarOpen(false)
  }, [activeId])

  return (
    <div className="flex h-screen overflow-hidden bg-slate-50">
      {/* 移动端遮罩 */}
      {sidebarOpen && (
        <div
          className="fixed inset-0 z-30 bg-slate-900/30 lg:hidden"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      {/* A. 数据上传侧栏 */}
      <aside
        className={`thin-scroll fixed inset-y-0 left-0 z-40 w-80 shrink-0 transform overflow-y-auto border-r border-slate-200 bg-slate-50/60 px-4 py-5 transition-transform duration-200 lg:static lg:z-auto lg:translate-x-0 ${
          sidebarOpen ? 'translate-x-0' : '-translate-x-full'
        }`}
      >
        <BrandMark />
        <div className="mt-5">
          <UploadPanel />
        </div>
      </aside>

      {/* B. 对话主区 */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center gap-3 border-b border-slate-200 bg-white px-4 py-2.5 lg:hidden">
          <button
            type="button"
            onClick={() => setSidebarOpen(true)}
            className="flex h-8 w-8 items-center justify-center rounded-lg text-slate-500 hover:bg-slate-100"
            aria-label="打开数据源面板"
          >
            <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="3" y1="6" x2="21" y2="6" />
              <line x1="3" y1="12" x2="21" y2="12" />
              <line x1="3" y1="18" x2="21" y2="18" />
            </svg>
          </button>
          <span className="text-sm font-semibold text-slate-700">DataSage</span>
        </header>

        <ChatMessages />
        <ChatInput />
      </div>

      {/* C. 数字溯源证据抽屉 */}
      <EvidencePanel />
    </div>
  )
}


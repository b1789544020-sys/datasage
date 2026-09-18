/**
 * DataSage 全局会话状态（Zustand）。
 *
 * 管理：数据源列表 / 当前数据源（含完整 schema）、聊天消息（用户问题 +
 * 每次分析的 steps/answer/result）、loading 与错误、以及证据抽屉的当前选中数字。
 */
import { create } from 'zustand'

import api from '../api/client'
import type {
  DatasourceDetail,
  DatasourceSummary,
  NumberVerdict,
  QueryResponse,
  ResultData,
} from '../api/types'

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  /** 用户问题（role=user） */
  question?: string
  /** 完整分析响应（role=assistant） */
  query?: QueryResponse
  /** 后端报错时挂在 assistant 气泡上 */
  error?: string
}

export interface EvidenceTarget {
  verdict: NumberVerdict
  resultData: ResultData
  question: string
}

interface ChatState {
  datasources: DatasourceSummary[]
  activeDatasource: DatasourceDetail | null
  listLoading: boolean
  uploading: boolean
  querying: boolean
  datasourceError: string | null
  queryError: string | null
  messages: ChatMessage[]
  evidence: EvidenceTarget | null

  hydrate: () => Promise<void>
  selectDatasource: (id: number) => Promise<void>
  uploadFile: (file: File) => Promise<void>
  loadSample: () => Promise<void>
  ask: (question: string) => Promise<void>
  openEvidence: (
    verdict: NumberVerdict,
    resultData: ResultData,
    question: string,
  ) => void
  closeEvidence: () => void
  clearMessages: () => void
}

function uid(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return crypto.randomUUID()
  }
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`
}

function errorMessage(exc: unknown, fallback: string): string {
  if (exc instanceof Error && exc.message) return exc.message
  return fallback
}

export const useChatStore = create<ChatState>((set, get) => ({
  datasources: [],
  activeDatasource: null,
  listLoading: false,
  uploading: false,
  querying: false,
  datasourceError: null,
  queryError: null,
  messages: [],
  evidence: null,

  hydrate: async () => {
    set({ listLoading: true, datasourceError: null })
    try {
      const datasources = await api.listDataSources()
      set({ datasources, listLoading: false })
      // 默认选中最近一个 ready 的数据源（拉完整 schema）
      const latest = datasources.find((d) => d.status === 'ready')
      if (latest) {
        await get().selectDatasource(latest.id)
      } else {
        set({ listLoading: false })
      }
    } catch (exc) {
      set({
        listLoading: false,
        datasourceError: errorMessage(exc, '数据源列表加载失败'),
      })
    }
  },

  selectDatasource: async (id: number) => {
    set({ datasourceError: null })
    try {
      const detail = await api.getDataSource(id)
      // 切换数据源后旧问答属于另一份数据，连同证据抽屉一起重置
      set({
        activeDatasource: detail,
        messages: [],
        evidence: null,
        queryError: null,
      })
    } catch (exc) {
      set({ datasourceError: errorMessage(exc, '数据源详情加载失败') })
    }
  },

  uploadFile: async (file: File) => {
    set({ uploading: true, datasourceError: null })
    try {
      const summary = await api.uploadDataSource(file)
      const [detail, datasources] = await Promise.all([
        api.getDataSource(summary.id),
        api.listDataSources(),
      ])
      set({
        activeDatasource: detail,
        datasources,
        uploading: false,
        messages: [],
        evidence: null,
      })
    } catch (exc) {
      set({
        uploading: false,
        datasourceError: errorMessage(exc, '文件上传失败'),
      })
    }
  },

  loadSample: async () => {
    set({ uploading: true, datasourceError: null })
    try {
      const summary = await api.createSampleDataSource()
      const [detail, datasources] = await Promise.all([
        api.getDataSource(summary.id),
        api.listDataSources(),
      ])
      set({
        activeDatasource: detail,
        datasources,
        uploading: false,
        messages: [],
        evidence: null,
      })
    } catch (exc) {
      set({
        uploading: false,
        datasourceError: errorMessage(exc, '示例数据加载失败'),
      })
    }
  },

  ask: async (question: string) => {
    const trimmed = question.trim()
    const active = get().activeDatasource
    if (!trimmed || get().querying) return
    if (!active) {
      set({ queryError: '请先上传或选择一个数据源' })
      return
    }

    // 多轮对话：取当前对话流里最后一轮成功回答的会话 id 作为追问锚点
    const prevSessionId = [...get().messages]
      .reverse()
      .find((m) => m.role === 'assistant' && m.query?.status === 'done')
      ?.query?.session_id

    const userMessage: ChatMessage = {
      id: uid(),
      role: 'user',
      question: trimmed,
    }
    set((state) => ({
      messages: [...state.messages, userMessage],
      querying: true,
      queryError: null,
    }))

    try {
      const query = await api.createQuery(active.id, trimmed, prevSessionId)
      set((state) => ({
        messages: [
          ...state.messages,
          { id: uid(), role: 'assistant', query },
        ],
        querying: false,
      }))
    } catch (exc) {
      set((state) => ({
        messages: [
          ...state.messages,
          {
            id: uid(),
            role: 'assistant',
            error: errorMessage(exc, '分析失败，请稍后重试'),
          },
        ],
        querying: false,
        queryError: errorMessage(exc, '分析失败'),
      }))
    }
  },

  openEvidence: (verdict, resultData, question) =>
    set({ evidence: { verdict, resultData, question } }),

  closeEvidence: () => set({ evidence: null }),

  clearMessages: () => set({ messages: [], queryError: null }),
}))

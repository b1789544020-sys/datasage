/**
 * DataSage 前端 API 客户端。
 *
 * 约定：
 * - 默认同源（Vite dev server 把 /api 代理到 http://localhost:8000），
 *   也可用 VITE_API_BASE_URL 指向独立部署的后端；
 * - FormData 请求不手动设置 Content-Type，交给浏览器补 boundary；
 * - 非 2xx 响应统一抛出 ApiError（detail 取自后端 {detail} 响应体）。
 */
import type {
  DatasourceDetail,
  DatasourceSummary,
  QueryResponse,
  SessionDetail,
} from './types'

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? ''

export class ApiError extends Error {
  status: number

  constructor(status: number, detail: string) {
    super(detail)
    this.name = 'ApiError'
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const isForm = init?.body instanceof FormData
  const headers = new Headers(init?.headers)
  if (!isForm && init?.body !== undefined && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }

  const response = await fetch(`${API_BASE_URL}${path}`, { ...init, headers })

  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`
    try {
      const data = (await response.json()) as { detail?: unknown }
      if (typeof data.detail === 'string') {
        detail = data.detail
      }
    } catch {
      // 非 JSON 错误体，保留 HTTP 状态文案
    }
    throw new ApiError(response.status, detail)
  }

  return (await response.json()) as T
}

export const api = {
  /** 后端健康检查 */
  getHealth: () => request<{ status: string }>('/health'),

  /** 数据源列表（概要，不含 schema） */
  listDataSources: () =>
    request<DatasourceSummary[]>('/api/v1/datasources', { method: 'GET' }),

  /** 数据源详情（含完整 schema） */
  getDataSource: (id: number) =>
    request<DatasourceDetail>(`/api/v1/datasources/${id}`),

  /** 上传 CSV / Excel（multipart/form-data） */
  uploadDataSource: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<DatasourceSummary>('/api/v1/datasources', {
      method: 'POST',
      body: form,
    })
  },

  /** 一键加载后端内置示例数据 */
  createSampleDataSource: () =>
    request<DatasourceSummary>('/api/v1/datasources/sample', {
      method: 'POST',
    }),

  /** 发起自然语言提问；prevSessionId 非空时作为多轮追问，带上一轮会话 id */
  createQuery: (
    datasourceId: number,
    question: string,
    prevSessionId?: number,
  ) =>
    request<QueryResponse>('/api/v1/query', {
      method: 'POST',
      body: JSON.stringify({
        datasource_id: datasourceId,
        question,
        ...(prevSessionId ? { prev_session_id: prevSessionId } : {}),
      }),
    }),

  /** 会话溯源详情 */
  getSession: (sessionId: number) =>
    request<SessionDetail>(`/api/v1/sessions/${sessionId}`),

  /** 导出某次会话的 Markdown 分析报告（纯文本下载） */
  getQueryReport: async (sessionId: number): Promise<string> => {
    const response = await fetch(
      `${API_BASE_URL}/api/v1/sessions/${sessionId}/report`,
    )
    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`
      try {
        const data = (await response.json()) as { detail?: unknown }
        if (typeof data.detail === 'string') {
          detail = data.detail
        }
      } catch {
        // 非 JSON 错误体，保留 HTTP 状态文案
      }
      throw new ApiError(response.status, detail)
    }
    return response.text()
  },
}

export default api


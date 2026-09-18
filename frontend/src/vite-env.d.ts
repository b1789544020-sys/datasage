/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 后端 API 基础地址，默认同源（走 Vite 代理），如 http://localhost:8000 */
  readonly VITE_API_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

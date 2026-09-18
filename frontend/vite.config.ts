import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 后端地址：本地开发默认 localhost:8000；Docker Compose 下通过
// VITE_PROXY_TARGET=http://backend:8000 指向 compose 网络里的后端容器
const proxyTarget = process.env.VITE_PROXY_TARGET ?? 'http://localhost:8000'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    host: true,
    proxy: {
      // 开发环境把后端请求代理到 FastAPI
      '/api': {
        target: proxyTarget,
        changeOrigin: true,
      },
      '/health': {
        target: proxyTarget,
        changeOrigin: true,
      },
    },
  },
})

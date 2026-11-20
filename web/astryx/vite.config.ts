import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// V1.3 换栈：React 中台与旧零构建控制台并存。
// dev 下所有 /api 与 /healthz 经代理打到既有 FastAPI(8101)，
// 因此前端不需要 CORS、不需要改造后端，也不复制任何 mock 数据。
export default defineConfig({
  plugins: [react()],
  base: './',
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8101', changeOrigin: true },
      '/healthz': { target: 'http://127.0.0.1:8101', changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 1600,
  },
})

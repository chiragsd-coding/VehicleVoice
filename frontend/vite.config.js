import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev-only config. During `npm run dev` the Vite dev server (port 5173)
// proxies API calls to a locally-running FastAPI backend (e.g.
//   .venv/bin/uvicorn backend.main:app --port 8000
// ). The PRODUCTION path needs no proxy: the backend serves frontend/dist
// directly on the live site.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8000',
      '/health': 'http://127.0.0.1:8000',
    },
  },
})
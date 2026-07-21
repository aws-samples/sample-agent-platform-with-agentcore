import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@': path.resolve(__dirname, 'src') },
  },
  server: {
    port: 5173,
    proxy: {
      // Local dev: forward API calls to the FastAPI backend
      '/api': 'http://localhost:8000',
      '/health': 'http://localhost:8000',
    },
  },
})

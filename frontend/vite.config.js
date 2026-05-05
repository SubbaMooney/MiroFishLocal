import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import path from 'path'

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src'),
      '@locales': path.resolve(__dirname, '../locales')
    }
  },
  server: {
    port: 3000,
    open: true,
    proxy: {
      // L10 (Audit): kein 'secure: false' — Target ist http (kein TLS),
      // der Vite-Default 'secure: true' greift nur fuer https-Targets.
      // Falls jemand das Target auf https aendert, soll der TLS-Check
      // aktiv bleiben.
      '/api': {
        target: 'http://localhost:5001',
        changeOrigin: true
      }
    }
  }
})

import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../static',
    // static/ holds only build output; clear it so stale bundles don't pile up.
    emptyOutDir: true,
  },
  base: '/',
})

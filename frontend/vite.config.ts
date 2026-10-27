import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const apiBase = env.VITE_API_BASE_URL || 'http://127.0.0.1:8000';
  return {
    plugins: [react()],
    server: {
      host: '127.0.0.1',
      port: 5173,
      strictPort: true,
      proxy: {
        '/api': apiBase,
        '/ws': { target: apiBase.replace(/^http/, 'ws'), ws: true },
      },
    },
    test: { environment: 'jsdom', setupFiles: './src/test-setup.ts' },
  };
});

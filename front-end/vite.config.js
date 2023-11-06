import legacy from '@vitejs/plugin-legacy'
import { resolve } from 'path'

export default {
  root: './src',
  base: '/static',
  publicDir: '../assets',
  build: {
    outDir: '../dist',
    rollupOptions: {
      input: {
        index: resolve(__dirname, 'src/index.js'),
        stats: resolve(__dirname, 'src/stats/index.html'),
      },
    },
  },
  plugins: [
    legacy({
      targets: ['defaults', 'not IE 11'],
    }),
  ]
}

import legacy from '@vitejs/plugin-legacy'
import { resolve } from 'path'

export default {
  root: './src',
  publicDir: '../assets',
  build: {
    outDir: '../dist',
    rollupOptions: {
      input: {
        main: resolve(__dirname, 'src/index.html'),
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

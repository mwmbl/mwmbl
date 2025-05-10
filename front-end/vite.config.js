import legacy from '@vitejs/plugin-legacy'
import { resolve } from 'path'

export default {
  root: './src',
  base: '/static',
  publicDir: '../assets',
  build: {
    outDir: '../dist',
    manifest: true,
    rollupOptions: {
      input: {
        index: resolve(__dirname, 'src/index.js'),
        stats: resolve(__dirname, 'src/stats/index.html'),
        terms: resolve(__dirname, 'src/terms-and-conditions/index.html'),
        privacy: resolve(__dirname, 'src/data-privacy/index.html'),
      },
    },
    minify: false,
  },
  plugins: [
    legacy({
      targets: ['defaults', 'not IE 11'],
    }),
  ]
}

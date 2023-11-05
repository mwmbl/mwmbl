import legacy from '@vitejs/plugin-legacy'

export default {
  root: './src',
  base: '/static',
  publicDir: '../assets',
  build: {
      outDir: '../dist',
      minify: false
  },
  plugins: [
    legacy({
      targets: ['defaults', 'not IE 11'],
    }),
  ]
}

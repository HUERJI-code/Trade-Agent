// esbuild 把 JSX、CSS 和依赖编译为浏览器文件；兼容本机 Node 20.17。
import { build } from 'esbuild'; // 构建器的 JavaScript API。
import { mkdir, copyFile } from 'node:fs/promises'; // 只写本项目的 dist 目录。
import { fileURLToPath } from 'node:url'; // 正确处理 Windows 路径与目录中的空格。
const root = fileURLToPath(new URL('.', import.meta.url)); // 当前文件所在的 frontend 目录。
await mkdir(new URL('./dist/assets/', import.meta.url), { recursive: true }); // 创建输出目录。
await build({
  absWorkingDir: root, // 相对入口都从 frontend 解析。
  entryPoints: ['src/main.jsx'], // React 程序入口。
  bundle: true, // 把本地 npm 依赖一起打包，网页不访问 CDN。
  outfile: 'dist/assets/app.js', // CSS 同时输出为 app.css。
  minify: true, // 缩小构建文件，源码中的教学注释保留在 src 中。
  sourcemap: false, // 学习时直接读源码，网页不需要源码映射文件。
  jsx: 'automatic', // 使用 React 的 JSX 编译方式。
  define: { 'process.env.NODE_ENV': '"production"' }, // 选择 React 正式运行版本。
  target: ['es2022'], // 面向现代本机浏览器。
});
await copyFile(new URL('./index.html', import.meta.url), new URL('./dist/index.html', import.meta.url)); // 复制网页外壳。
console.log('React 页面已构建到 frontend/dist。'); // 成功后再启动 FastAPI。

import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import tailwindcss from "@tailwindcss/vite";

// 端口刻意避开 Windows 保留段（1346-1445 / 1046-1145 等）和旧项目占用的 57890-57895。
export default defineConfig({
  plugins: [vue(), tailwindcss()],
  server: {
    // 显式绑 IPv4：默认 localhost 在部分 Windows 环境只监听 ::1，
    // Playwright 用 127.0.0.1 探活会直接连接失败。
    host: "127.0.0.1",
    port: 57911,
    strictPort: true,
    proxy: {
      "/api": {
        target: process.env.CHAIERWW_API ?? "http://127.0.0.1:57910",
        changeOrigin: false,
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});

/// <reference types="vite/client" />

/**
 * 构建期虚拟模块：组件注册表的体检结果。
 *
 * 由 `vite.config.ts` 的 `keelReport()` 插件生成，数据来自 `keel/components.json`
 * 与 `web/src/components/` 的真实文件 —— 新增组件不需要改这里，也不用改页面。
 */
declare module "virtual:keel-report" {
  const report: {
    /** 目录里有、注册表没有。 */
    unregistered: string[];
    /** 注册表有、但没有任何源码 import 它。 */
    unused: string[];
  };
  export default report;
}

interface ImportMetaEnv {
  /** 可选覆盖构建期形态（`dev` / `prod`）；不设置时按 vite dev / build 推断。 */
  readonly VITE_APP_MODE?: string;
}

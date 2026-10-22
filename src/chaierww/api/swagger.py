"""自建 `/docs`：同源 iframe 内嵌的 Swagger UI。

为什么不用框架自带的 `docs_url`：
- 自带页从 CDN 拉资源，离线/内网环境直接白屏；
- 页面壳不可控，没法跟随应用主题、也没法改标题与说明；
- 需要自己控制 `X-Frame-Options`，否则同源 iframe 也会被 DENY 掉。

所以这里返回一份自己的 HTML，资源全部走本地 `/docs-assets`。

**鉴权说明**：chaierww 只监听 127.0.0.1 且本身没有 token 鉴权，
因此不声明 `securitySchemes`（声明了反而会让人以为要填 Bearer）。
将来若引入访问令牌，需要同时补：
`app.openapi_schema["components"]["securitySchemes"] = {"BearerAuth": {...}}`
以及 `app.openapi_schema["security"] = [{"BearerAuth": []}]`，
否则 Swagger 没有 Authorize 按钮，文档只能看不能调。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

DISABLE_ENV = "CHAIERWW_DISABLE_DOCS"
ASSETS_URL = "/docs-assets"
OPENAPI_URL = "/api/openapi.json"

ASSETS_DIR = Path(__file__).resolve().parent.parent / "webassets" / "swagger"

_PAGE = """<!doctype html>
<html lang="zh-CN" data-theme="light">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>chaierww · 接口文档</title>
<link rel="icon" href="{assets}/favicon-32x32.png" />
<link rel="stylesheet" href="{assets}/swagger-ui.css" />
<style>
  :root {{ --docs-bg: #ffffff; --docs-ink: #1c1e1c; --docs-line: #e5e7e4; --docs-muted: #767b74; }}
  html[data-theme="dark"] {{
    --docs-bg: #16181a; --docs-ink: #e8eae7; --docs-line: #2c3033; --docs-muted: #9aa09b;
  }}
  html, body {{ margin: 0; background: var(--docs-bg); color: var(--docs-ink); }}
  body {{ font-family: "Segoe UI", "Microsoft YaHei", system-ui, sans-serif; }}
  .docs-note {{
    display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap;
    padding: 10px 20px; border-bottom: 1px solid var(--docs-line);
    font-size: 12.5px; color: var(--docs-muted);
  }}
  .docs-note b {{ color: var(--docs-ink); font-weight: 600; }}
  .docs-note code {{ font-family: "Cascadia Code", Consolas, monospace; font-size: 11.5px; }}
  /* Swagger 默认配色是浅色，深色主题下只调容器，避免半深半浅 */
  html[data-theme="dark"] .swagger-ui,
  html[data-theme="dark"] .swagger-ui .info .title {{ color: var(--docs-ink); }}
  html[data-theme="dark"] .swagger-ui .opblock-tag,
  html[data-theme="dark"] .swagger-ui .opblock-summary-description {{
    color: var(--docs-ink);
  }}
  html[data-theme="dark"] .swagger-ui .scheme-container,
  html[data-theme="dark"] .swagger-ui .opblock {{ background: transparent; box-shadow: none; }}
</style>
</head>
<body>
  <div class="docs-note">
    <b>chaierww 接口文档</b>
    <span>运行期从 <code>{openapi}</code> 生成，接口变了这里自动跟着变。</span>
    <span>只监听本机，无需鉴权。</span>
  </div>
  <div id="swagger-ui"></div>
  <script src="{assets}/swagger-ui-bundle.js"></script>
  <script>
    // 主题跟随：与父页面共用 localStorage 键，父页面切换时 iframe 同步。
    (function () {{
      var KEY = "chaierww-theme";
      function apply(theme) {{
        document.documentElement.dataset.theme = theme === "dark" ? "dark" : "light";
      }}
      try {{ apply(localStorage.getItem(KEY)); }} catch (e) {{ apply("light"); }}
      window.addEventListener("storage", function (event) {{
        if (event.key === KEY) apply(event.newValue);
      }});
    }})();

    window.addEventListener("load", function () {{
      if (!window.SwaggerUIBundle) {{
        document.getElementById("swagger-ui").textContent =
          "Swagger UI 静态资源缺失，请检查 webassets/swagger。";
        return;
      }}
      window.ui = SwaggerUIBundle({{
        url: "{openapi}",
        dom_id: "#swagger-ui",
        presets: [SwaggerUIBundle.presets.apis],
        layout: "BaseLayout",
        deepLinking: true,
        docExpansion: "none",
        defaultModelsExpandDepth: 0,
        displayRequestDuration: true,
        tryItOutEnabled: true,
        persistAuthorization: true,
        filter: true,
      }});
    }});
  </script>
</body>
</html>
"""


def docs_disabled() -> bool:
    return os.environ.get(DISABLE_ENV, "").strip() in {"1", "true", "yes"}


def mount_swagger(app: FastAPI) -> None:
    """挂载 `/docs` 与它依赖的静态资源。被禁用时两个都不注册。"""
    from fastapi.staticfiles import StaticFiles

    router = APIRouter(tags=["文档"])

    @router.get("/api/docs/status", summary="接口文档是否可用", tags=["文档"])
    def docs_status() -> dict[str, Any]:
        """前端先问这里，再决定要不要挂 iframe。

        不探测的话，禁用状态下 iframe 会落到 SPA catch-all 拿到 index.html，
        表现成「文档页里套着整个应用」。
        """
        available = not docs_disabled() and (ASSETS_DIR / "swagger-ui-bundle.js").is_file()
        reason = ""
        if docs_disabled():
            reason = f"接口文档已通过 {DISABLE_ENV} 关闭"
        elif not available:
            reason = "缺少本地 Swagger UI 资源（src/chaierww/webassets/swagger）"
        return {
            "available": available,
            "docsUrl": "/docs",
            "openapiUrl": OPENAPI_URL,
            "reason": reason,
        }

    if docs_disabled():
        # 显式 404，而不是让 /docs 落到 SPA 兜底。
        # 落到兜底会返回 index.html，表现为「文档页里套着整个应用」。
        @router.get("/docs", include_in_schema=False)
        def docs_disabled_page() -> JSONResponse:
            return JSONResponse(
                {"detail": f"接口文档已关闭（{DISABLE_ENV}=1）"},
                status_code=404,
                headers={"Cache-Control": "no-cache"},
            )

        app.include_router(router)
        return

    if not (ASSETS_DIR / "swagger-ui-bundle.js").is_file():
        # 资源缺失时宁可让 /docs 404，也不要把 SPA 的 index.html 兜进来。
        app.include_router(router)
        return

    app.mount(
        ASSETS_URL,
        StaticFiles(directory=ASSETS_DIR),
        name="docs-assets",
    )

    @app.get("/docs", include_in_schema=False)
    def swagger_page(_request: Request) -> HTMLResponse:
        return HTMLResponse(
            _PAGE.format(assets=ASSETS_URL, openapi=OPENAPI_URL),
            headers={
                # 同源 iframe 必须放行，默认的 DENY 会让设置里的文档 tab 白屏。
                "X-Frame-Options": "SAMEORIGIN",
                # 改完代码刷新即生效，不缓存这份壳。
                "Cache-Control": "no-cache",
            },
        )

    app.include_router(router)

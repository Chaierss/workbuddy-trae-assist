"""文档服务：把仓库里的 Markdown 与 OpenAPI 规格暴露给前端。

两条数据都是**运行期读取**，所以新增 md 文件或新增接口之后，
刷新页面就能看到，不需要重新构建前端，也不需要维护第二份清单。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

DOCS_ENV = "CHAIERWW_DOCS"
MAX_DOC_BYTES = 512 * 1024


def _docs_root() -> Path | None:
    override = os.environ.get(DOCS_ENV)
    if override:
        candidate = Path(override).expanduser()
        return candidate if candidate.is_dir() else None

    # src/chaierww/api/docs.py -> 仓库根
    candidate = Path(__file__).resolve().parents[3] / "docs"
    return candidate if candidate.is_dir() else None


def _title_of(path: Path) -> str:
    """取第一个一级标题作为显示名，取不到就退回文件名。"""
    try:
        with open(path, "rb") as handle:
            head = handle.read(4096).decode("utf-8", "replace")
    except OSError:
        return path.stem
    for line in head.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip() or path.stem
    return path.stem


def _type_of(schema: dict[str, Any] | None) -> str:
    """把 JSON Schema 归成一个可读的类型串。"""
    if not schema:
        return ""
    # 可选参数在 pydantic v2 里会被包成 anyOf: [{"type": "string"}, {"type": "null"}]
    for branch in schema.get("anyOf", []) or []:
        if isinstance(branch, dict) and branch.get("type") not in {None, "null"}:
            return str(branch["type"])
    if "type" in schema:
        return str(schema["type"])
    if "$ref" in schema:
        return str(schema["$ref"]).rsplit("/", 1)[-1]
    return ""


def _group_of(relative: Path) -> str:
    return relative.parent.as_posix() if relative.parent.as_posix() != "." else ""


def build_docs_router() -> APIRouter:
    router = APIRouter(prefix="/api/docs", tags=["文档"])

    @router.get("", summary="列出 docs 目录下的 Markdown", tags=["文档"])
    def list_docs(response: Response) -> dict[str, Any]:
        # 改完 md 刷新即生效，别让浏览器缓存住目录。
        response.headers["Cache-Control"] = "no-cache"
        root = _docs_root()
        if root is None:
            return {"root": None, "files": []}

        files = []
        for path in sorted(root.rglob("*.md")):
            if not path.is_file():
                continue
            relative = path.relative_to(root)
            stat = path.stat()
            files.append(
                {
                    "path": relative.as_posix(),
                    "title": _title_of(path),
                    "group": _group_of(relative),
                    "size": stat.st_size,
                    "updatedAt": datetime.fromtimestamp(stat.st_mtime, UTC)
                    .replace(microsecond=0)
                    .isoformat(),
                }
            )
        return {"root": str(root), "files": files}

    @router.get("/content", summary="读取指定 Markdown 正文", tags=["文档"])
    def read_doc(path: str, response: Response) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-cache"
        root = _docs_root()
        if root is None:
            raise HTTPException(status_code=404, detail="文档目录不存在")

        resolved_root = root.resolve()
        target = (resolved_root / path).resolve()

        # 只允许读取 docs 目录内的 .md 文件，挡住 ../ 穿越与任意文件读取。
        if not target.is_relative_to(resolved_root):
            raise HTTPException(status_code=400, detail="非法路径")
        if target.suffix.lower() != ".md":
            raise HTTPException(status_code=400, detail="只支持 Markdown 文档")
        if not target.is_file():
            raise HTTPException(status_code=404, detail=f"文档不存在：{path}")
        if target.stat().st_size > MAX_DOC_BYTES:
            raise HTTPException(status_code=413, detail="文档过大，请拆分后再查看")

        return {
            "path": target.relative_to(resolved_root).as_posix(),
            "title": _title_of(target),
            "content": target.read_text(encoding="utf-8", errors="replace"),
        }

    @router.get("/openapi", summary="接口清单（结构化视图）", tags=["文档"])
    def openapi_summary(request: Request) -> dict[str, Any]:
        """直接从应用自身生成，接口变了这里自动跟着变。"""
        spec = request.app.openapi()
        endpoints = []
        for raw_path, methods in spec.get("paths", {}).items():
            for method, operation in methods.items():
                if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                    continue
                parameters = [
                    {
                        "name": param.get("name", ""),
                        "in": param.get("in", ""),
                        "required": bool(param.get("required")),
                        "type": _type_of(param.get("schema")),
                        "description": param.get("description", ""),
                    }
                    for param in operation.get("parameters", [])
                ]
                endpoints.append(
                    {
                        "method": method.upper(),
                        "path": raw_path,
                        "summary": operation.get("summary") or operation.get("operationId", ""),
                        "tags": operation.get("tags", []),
                        "parameters": parameters,
                        "hasBody": bool(operation.get("requestBody")),
                    }
                )
        endpoints.sort(key=lambda item: (item["path"], item["method"]))
        return {
            "title": spec.get("info", {}).get("title", "API"),
            "version": spec.get("info", {}).get("version", ""),
            "count": len(endpoints),
            "endpoints": endpoints,
        }

    return router

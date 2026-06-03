# Copyright (c) Opendatalab. All rights reserved.
"""
阿里云文档智能（Document Mind）— 文档解析大模型版 客户端封装。

作为 Azure Document Intelligence 的首选替换方案，使用 DocMind 文档解析大模型版 API
进行文档结构化和 Markdown 转换。

与 Azure DI prebuilt-layout 高度一致:
  - 原生 PDF 支持（无需转图片）
  - 返回 Markdown 格式内容
  - 结构化元素: 段落、表格(含 HTML)、图片区域、标题层级、章节结构
  - 阅读顺序保留

API 文档: https://help.aliyun.com/zh/document-mind/developer-reference/document-parsing-large-model-version

调用流程（异步任务模式）:
  1. SubmitDocParserJobAdvance — 提交本地文件
  2. QueryDocParserStatus     — 轮询任务状态 (Success/Fail)
  3. GetDocParserResult       — 获取结构化结果

环境变量 (.env):
    ALIBABA_CLOUD_ACCESS_KEY_ID      阿里云 AccessKey ID
    ALIBABA_CLOUD_ACCESS_KEY_SECRET  阿里云 AccessKey Secret
    ALIYUN_DOCMIND_ENDPOINT          API 端点 (可选，默认 docmind-api.cn-hangzhou.aliyuncs.com)
"""

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from io import BytesIO
from typing import Optional

try:
    from dotenv import load_dotenv, find_dotenv
    _env_path = find_dotenv(usecwd=True)
    if _env_path:
        load_dotenv(_env_path)
except ImportError:
    pass

from loguru import logger

from mineru.backend.rag.azure_doc_intelligence import DocumentAnalysisResult

# ── 配置常量 ──────────────────────────────────────────────
ALIYUN_AK_ID_ENV = "ALIBABA_CLOUD_ACCESS_KEY_ID"
ALIYUN_AK_SECRET_ENV = "ALIBABA_CLOUD_ACCESS_KEY_SECRET"
ALIYUN_DOCMIND_ENDPOINT_ENV = "ALIYUN_DOCMIND_ENDPOINT"
ALIYUN_DOCMIND_DEFAULT_ENDPOINT = "docmind-api.cn-hangzhou.aliyuncs.com"
ALIYUN_DOCMIND_REGION = "cn-hangzhou"

DOCMIND_POLL_INTERVAL = 2.0       # 轮询间隔 (秒)
DOCMIND_POLL_TIMEOUT = 300.0      # 轮询超时 (秒)
DOCMIND_MAX_RETRIES = 3           # 提交/获取失败时的重试次数
DOCMIND_PDF_LIMIT_MB = 100        # PDF 大小限制 (MB)
DOCMIND_PAGE_LIMIT = 1000         # 页数限制

# SDK 版本: alibabacloud_docmind_api20220711 >= 1.2.1
DOCMIND_API_MODULE = "alibabacloud_docmind_api20220711"
DOCMIND_CLIENT = f"{DOCMIND_API_MODULE}.client"
DOCMIND_MODELS = f"{DOCMIND_API_MODULE}.models"


# ── 坐标工具 ──────────────────────────────────────────────

def _pos_to_bbox(pos: list[dict]) -> list[int]:
    """四点坐标 [{x,y},...] → [x0, y0, x1, y1]"""
    if not pos or len(pos) < 4:
        return [0, 0, 0, 0]
    xs = [p.get("x", p.get("X", 0)) for p in pos]
    ys = [p.get("y", p.get("Y", 0)) for p in pos]
    return [int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))]


def _safe_get(d: dict, *keys, default=None):
    """安全获取嵌套字典值 (多 key 变体尝试)"""
    for key in keys:
        val = d.get(key)
        if val is not None:
            return val
    return default


# ── GetDocParserResult → DocumentAnalysisResult 映射 ─────

def _parse_docmind_result(result_data: dict) -> DocumentAnalysisResult:
    """
    将 DocMind GetDocParserResult 的 Data 字段映射为标准 DocumentAnalysisResult。

    DocMind 大模型版返回的结构 (推断基于 API 模式):
      Data: {
        "Id": str,                   # 任务 ID
        "markdown": str,             # 全文 Markdown
        "elements": [                # 结构化元素
          {
            "Type": "paragraph" | "table" | "figure" | "heading",
            "Text": str,
            "PageNum": int,
            "BoundingBox": {...},
            ...
          }
        ],
        "tables": [...],            # 表格详情 (含 HTML)
        "figures": [...],           # 图片/图表详情
      }

    注意: 精确字段名取决于 SDK 版本，这里使用多候选键名做容错解析。
    """
    result = DocumentAnalysisResult()
    result.raw_result = result_data

    if not result_data:
        return result

    # ── 元数据 ────────────────────────────────────────
    result.metadata = {
        "model_id": "DocMind-DocParser-Large",
        "api_version": "2022-07-11",
        "content_format": "markdown",
        "provider": "aliyun_docmind",
        "job_id": result_data.get("Id", result_data.get("id", "")),
    }

    # ── 元素列表 ─────────────────────────────────────
    elements = (
        result_data.get("elements")
        or result_data.get("Elements")
        or result_data.get("elementContents")
        or []
    )
    if not elements:
        # 尝试从 parsedDocument 路径获取
        parsed = result_data.get("parsedDocument", result_data.get("ParsedDocument", {}))
        elements = (
            parsed.get("elements")
            or parsed.get("Elements")
            or parsed.get("elementContents")
            or []
        )

    # ── 全文 Markdown (备选: 从元素拼接) ────────────
    full_markdown = result_data.get(
        "markdown", result_data.get("Markdown",
        result_data.get("content", result_data.get("Content", "")))
    )

    # ── 按元素类型分组 ─────────────────────────────
    pages_map: dict[int, dict] = {}
    all_paragraphs = []
    all_tables = []
    all_figures = []

    for idx, elem in enumerate(elements):
        if not isinstance(elem, dict):
            continue

        elem_type = str(_safe_get(elem, "Type", "type", "ElementType", "elementType", default="paragraph")).lower()
        elem_text = str(_safe_get(elem, "Text", "text", "Content", "content", "Word", "word", default=""))
        page_num = int(_safe_get(elem, "PageNum", "pageNum", "PageNumber", "pageNumber", "Page", "page", default=1))
        bbox_raw = _safe_get(elem, "BoundingBox", "boundingBox", "BBox", "bbox", "Pos", "pos", "Position", "position")
        role = _safe_get(elem, "Role", "role", "HeadingLevel", "headingLevel")

        # 坐标转换
        bbox = [0, 0, 0, 0]
        if isinstance(bbox_raw, dict):
            x = bbox_raw.get("x", bbox_raw.get("X", 0))
            y = bbox_raw.get("y", bbox_raw.get("Y", 0))
            w = bbox_raw.get("w", bbox_raw.get("W", bbox_raw.get("width", bbox_raw.get("Width", 0))))
            h = bbox_raw.get("h", bbox_raw.get("H", bbox_raw.get("height", bbox_raw.get("Height", 0))))
            bbox = [int(x), int(y), int(x + w), int(y + h)]
            if w == 0 and h == 0:
                bbox = None
        elif isinstance(bbox_raw, list) and len(bbox_raw) == 4:
            bbox = [int(v) for v in bbox_raw]
        else:
            bbox = None

        # 确保页面存在
        if page_num not in pages_map:
            pages_map[page_num] = {
                "page_number": page_num,
                "width": 0, "height": 0, "unit": "pixel",
                "angle": 0, "lines": [],
            }

        # 按类型分发
        if elem_type in ("heading", "title", "subtitle", "sectionheading", "section_heading"):
            all_paragraphs.append({
                "content": elem_text,
                "role": role if role else "sectionHeading",
                "page_number": page_num,
                "bbox": bbox,
            })
        elif elem_type in ("paragraph", "text", "para", "p", "body"):
            all_paragraphs.append({
                "content": elem_text,
                "role": role if role else None,
                "page_number": page_num,
                "bbox": bbox,
            })
        elif elem_type in ("pageheader", "page_header", "header"):
            all_paragraphs.append({
                "content": elem_text,
                "role": "pageHeader",
                "page_number": page_num,
                "bbox": bbox,
            })
        elif elem_type in ("pagefooter", "page_footer", "footer"):
            all_paragraphs.append({
                "content": elem_text,
                "role": "pageFooter",
                "page_number": page_num,
                "bbox": bbox,
            })
        elif elem_type in ("footnote",):
            all_paragraphs.append({
                "content": elem_text,
                "role": "footnote",
                "page_number": page_num,
                "bbox": bbox,
            })
        elif elem_type in ("table",):
            # DocMind 表格元素通常附带 HTML 和子结构
            table_html = str(_safe_get(elem, "HTML", "html", "TableHTML", "tableHTML", default=""))
            cells = _safe_get(elem, "Cells", "cells", "CellInfos", "cellInfos", default=[])
            row_count = int(_safe_get(elem, "RowCount", "rowCount", default=0))
            col_count = int(_safe_get(elem, "ColCount", "colCount", "ColumnCount", "columnCount", default=0))

            table_info = {
                "row_count": row_count or len(cells) // (col_count or 1) if col_count else 0,
                "col_count": col_count,
                "cells": [],
                "page_numbers": [page_num],
                "caption": str(_safe_get(elem, "Caption", "caption", default="")) or None,
                "footnotes": [],
                "table_html": table_html,
            }

            # 转换单元格
            for cell in (cells if isinstance(cells, list) else []):
                if isinstance(cell, dict):
                    cbbox = bbox
                    cpos = _safe_get(cell, "BoundingBox", "boundingBox", "BBox", "bbox", "Pos", "pos")
                    if isinstance(cpos, dict):
                        cx = cpos.get("x", cpos.get("X", 0))
                        cy = cpos.get("y", cpos.get("Y", 0))
                        cw = cpos.get("w", cpos.get("W", cpos.get("width", 0)))
                        ch = cpos.get("h", cpos.get("H", cpos.get("height", 0)))
                        cbbox = [int(cx), int(cy), int(cx + cw), int(cy + ch)]
                    table_info["cells"].append({
                        "row_index": int(_safe_get(cell, "RowIndex", "rowIndex", "Row", "row", "ysc", default=0)),
                        "col_index": int(_safe_get(cell, "ColIndex", "colIndex", "Col", "col", "xsc", default=0)),
                        "row_span": int(_safe_get(cell, "RowSpan", "rowSpan", default=1)),
                        "col_span": int(_safe_get(cell, "ColSpan", "colSpan", default=1)),
                        "content": str(_safe_get(cell, "Text", "text", "Content", "content", "Word", "word", default="")),
                        "kind": str(_safe_get(cell, "Kind", "kind", default="data")),
                        "page_number": page_num,
                        "bbox": cbbox,
                    })

            all_tables.append(table_info)
        elif elem_type in ("figure", "image", "chart", "picture"):
            all_figures.append({
                "id": str(_safe_get(elem, "Id", "id", default=f"fig_{idx}")),
                "page_numbers": [page_num],
                "bbox": bbox,
                "caption": str(_safe_get(elem, "Caption", "caption", default="")) or None,
                "footnotes": [],
            })

    # ── 页面信息: 从元素中提取页面维度 ──────────────
    # 优先使用显式页面数据
    pages_data = (
        result_data.get("pages")
        or result_data.get("Pages")
        or result_data.get("pageInfo")
        or result_data.get("PageInfo")
        or []
    )
    if pages_data:
        for pg in pages_data:
            pn = int(_safe_get(pg, "PageNum", "pageNum", "PageNumber", "pageNumber", default=1))
            if pn in pages_map:
                pages_map[pn]["width"] = int(_safe_get(pg, "Width", "width", default=0))
                pages_map[pn]["height"] = int(_safe_get(pg, "Height", "height", default=0))
                pages_map[pn]["unit"] = str(_safe_get(pg, "Unit", "unit", default="pixel"))
                pages_map[pn]["angle"] = int(_safe_get(pg, "Angle", "angle", default=0))

    result.pages = sorted(pages_map.values(), key=lambda p: p["page_number"])
    result.paragraphs = all_paragraphs
    result.tables = [t for t in all_tables if t["cells"] or t["table_html"]]
    result.figures = all_figures
    result.sections = []  # DocMind 通过 heading 元素嵌入段落流，而非独立返回 sections

    # 页数修正
    if not result.pages and all_paragraphs:
        seen_pages = sorted(set(p["page_number"] for p in all_paragraphs))
        result.pages = [{"page_number": pn, "width": 0, "height": 0, "unit": "pixel", "angle": 0, "lines": []} for pn in seen_pages]
    result.metadata["page_count"] = len(result.pages)

    return result


# ── DocMind 客户端 ────────────────────────────────────────

class AliyunOCRClient:
    """
    阿里云文档解析大模型版客户端 (单例模式)。

    接口与 AzureDocumentIntelligenceClient 完全兼容，可直接替换使用。

    调用流程:
      1. 上传 PDF/文档 → SubmitDocParserJobAdvance
      2. 轮询状态 → QueryDocParserStatus (直到 Success/Fail)
      3. 获取结果 → GetDocParserResult
      4. 映射为 → DocumentAnalysisResult

    Usage:
        client = AliyunOCRClient()
        result = await client.analyze_document(pdf_bytes)
        result = client.analyze_document_sync(pdf_bytes)
    """

    _instance: Optional["AliyunOCRClient"] = None
    _lock = __import__('threading').RLock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(
        self,
        access_key_id: Optional[str] = None,
        access_key_secret: Optional[str] = None,
        endpoint: Optional[str] = None,
    ):
        with self._lock:
            if hasattr(self, '_initialized') and self._initialized:
                return
            self._ak_id = (
                access_key_id
                or os.getenv(ALIYUN_AK_ID_ENV, "")
            )
            self._ak_secret = (
                access_key_secret
                or os.getenv(ALIYUN_AK_SECRET_ENV, "")
            )
            self._endpoint = (
                endpoint
                or os.getenv(ALIYUN_DOCMIND_ENDPOINT_ENV, "")
                or ALIYUN_DOCMIND_DEFAULT_ENDPOINT
            )
            self._client = None
            self._poll_interval = DOCMIND_POLL_INTERVAL
            self._poll_timeout = DOCMIND_POLL_TIMEOUT
            self._initialized = True

    def _get_client(self):
        """获取或延迟初始化 DocMind Client"""
        if self._client is None:
            if not self._ak_id or not self._ak_secret:
                raise RuntimeError(
                    f"Alibaba Cloud credentials not configured. "
                    f"Set {ALIYUN_AK_ID_ENV} and {ALIYUN_AK_SECRET_ENV} in .env file."
                )
            try:
                from alibabacloud_tea_openapi.models import Config
                # DocMind SDK 包名
                import importlib
                client_mod = importlib.import_module(
                    f"{DOCMIND_API_MODULE}.client"
                )
                ClientCls = client_mod.Client
            except ImportError as e:
                raise ImportError(
                    "DocMind SDK not installed. "
                    "Install with: pip install alibabacloud_docmind_api20220711"
                ) from e

            config = Config(
                access_key_id=self._ak_id,
                access_key_secret=self._ak_secret,
                endpoint=self._endpoint,
                region_id=ALIYUN_DOCMIND_REGION,
            )
            self._client = ClientCls(config)
        return self._client

    # ── 作业提交流 ─────────────────────────────────

    def _submit_job(self, file_bytes: bytes, file_name: str = "document.pdf") -> str:
        """
        提交文档解析作业 (本地文件上传)。

        Returns:
            job_id (str) — 用于后续轮询和获取结果
        """
        client = self._get_client()
        models = __import__(f"{DOCMIND_API_MODULE}.models", fromlist=['*'])
        runtime_mod = __import__('alibabacloud_tea_util.models', fromlist=['RuntimeOptions'])

        request = models.SubmitDocParserJobAdvanceRequest(
            file_name=file_name,
        )
        # Advance 版本需要将 stream 赋值给特定字段
        # 字段名因 SDK 版本而异: file_url_object / file_url_object / file_content_object
        body_stream = BytesIO(file_bytes)
        # 尝试多个可能的字段名
        setattr(request, 'file_url_object', body_stream)

        runtime = runtime_mod.RuntimeOptions()

        last_error = None
        for attempt in range(DOCMIND_MAX_RETRIES):
            try:
                response = client.submit_doc_parser_job_advance(request, runtime)
                body = response.body

                if body is None:
                    raise RuntimeError("Empty response from SubmitDocParserJobAdvance")

                code = getattr(body, 'code', None) or getattr(body, 'Code', None)
                if code:
                    msg = getattr(body, 'message', '') or getattr(body, 'Message', '')
                    raise RuntimeError(f"DocMind submit error: {code} - {msg}")

                data = getattr(body, 'data', None) or getattr(body, 'Data', None)
                if data is None:
                    raise RuntimeError("No Data in submit response")

                job_id = getattr(data, 'id', None) or getattr(data, 'Id', None)
                if not job_id:
                    raise RuntimeError("No job ID in submit response")

                logger.info(f"DocMind job submitted: {job_id} ({file_name})")
                return str(job_id)

            except RuntimeError:
                raise
            except Exception as e:
                last_error = e
                logger.warning(
                    f"DocMind submit failed (attempt {attempt + 1}/{DOCMIND_MAX_RETRIES}): {e}"
                )
                if attempt < DOCMIND_MAX_RETRIES - 1:
                    time.sleep(DOCMIND_POLL_INTERVAL * (attempt + 1))
                    body_stream.seek(0)
                else:
                    raise RuntimeError(
                        f"DocMind submit failed after {DOCMIND_MAX_RETRIES} attempts: {last_error}"
                    ) from last_error

    def _poll_until_complete(self, job_id: str) -> str:
        """
        轮询任务状态直到完成或超时。

        Returns:
            "Success" — 处理成功；轮询到 "Fail" 时抛出异常
        """
        client = self._get_client()
        models = __import__(f"{DOCMIND_API_MODULE}.models", fromlist=['*'])

        request = models.QueryDocParserStatusRequest(id=job_id)
        start_time = time.time()

        while True:
            elapsed = time.time() - start_time
            if elapsed > self._poll_timeout:
                raise TimeoutError(
                    f"DocMind job {job_id} timed out after {self._poll_timeout:.0f}s"
                )

            try:
                response = client.query_doc_parser_status(request)
                body = response.body
            except Exception as e:
                logger.warning(f"DocMind poll error for {job_id}: {e}")
                time.sleep(self._poll_interval)
                continue

            if body is None:
                time.sleep(self._poll_interval)
                continue

            status = getattr(body, 'status', None) or getattr(body, 'Status', None)
            if status is None:
                # 尝试从 data 获取
                data = getattr(body, 'data', None) or getattr(body, 'Data', None)
                if data:
                    status = getattr(data, 'status', None) or getattr(data, 'Status', None)

            if not status:
                logger.warning(f"DocMind poll: no Status field in response for {job_id}")
                time.sleep(self._poll_interval)
                continue

            status_str = str(status)
            if status_str == "Success":
                logger.info(f"DocMind job {job_id}: Success (elapsed {elapsed:.1f}s)")
                return status_str
            elif status_str == "Fail":
                msg = (
                    getattr(body, 'message', '')
                    or getattr(body, 'Message', '')
                    or getattr(body, 'code', '')
                )
                raise RuntimeError(f"DocMind job {job_id} failed: {msg}")
            else:
                # Processing / Queued
                progress = ""
                data = getattr(body, 'data', None) or getattr(body, 'Data', None)
                if data:
                    num = getattr(data, 'NumberOfSuccessfulParsing', None)
                    if num is not None:
                        progress = f" ({num} modules done)"
                logger.debug(f"DocMind job {job_id}: {status_str}{progress}, elapsed {elapsed:.0f}s")
                time.sleep(self._poll_interval)

    def _get_result(self, job_id: str) -> dict:
        """获取解析结果"""
        client = self._get_client()
        models = __import__(f"{DOCMIND_API_MODULE}.models", fromlist=['*'])

        request = models.GetDocParserResultRequest(
            id=job_id,
        )

        last_error = None
        for attempt in range(DOCMIND_MAX_RETRIES):
            try:
                response = client.get_doc_parser_result(request)
                body = response.body

                if body is None:
                    raise RuntimeError("Empty response from GetDocParserResult")

                code = getattr(body, 'code', None) or getattr(body, 'Code', None)
                if code:
                    msg = getattr(body, 'message', '') or getattr(body, 'Message', '')
                    raise RuntimeError(f"DocMind get result error: {code} - {msg}")

                body_dict = body.to_map() if hasattr(body, 'to_map') else {}
                if not body_dict:
                    logger.warning("GetDocParserResult returned empty body dict")
                    return {}

                return body_dict

            except RuntimeError:
                raise
            except Exception as e:
                last_error = e
                logger.warning(
                    f"DocMind get result failed (attempt {attempt + 1}/{DOCMIND_MAX_RETRIES}): {e}"
                )
                if attempt < DOCMIND_MAX_RETRIES - 1:
                    time.sleep(DOCMIND_POLL_INTERVAL * (attempt + 1))
                else:
                    raise RuntimeError(
                        f"DocMind get result failed after {DOCMIND_MAX_RETRIES} attempts: {last_error}"
                    ) from last_error

    # ── 公开接口 ──────────────────────────────────────

    async def analyze_document(
        self,
        file_bytes: bytes,
        content_type: str = "application/pdf",
    ) -> DocumentAnalysisResult:
        """
        分析文档（PDF/Word/PPT/Excel/图片），返回结构化结果。

        内部流程 (异步):
          1. SubmitDocParserJobAdvance — 提交文档
          2. QueryDocParserStatus — 轮询等待
          3. GetDocParserResult — 获取结果
          4. _parse_docmind_result — 映射为标准结构

        Args:
            file_bytes: 文件字节流
            content_type: MIME 类型，用于推断文件名后缀

        Returns:
            DocumentAnalysisResult
        """
        # 推断文件名
        ext_map = {
            "application/pdf": ".pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
            "application/msword": ".doc",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
            "application/vnd.ms-powerpoint": ".ppt",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
            "application/vnd.ms-excel": ".xls",
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/bmp": ".bmp",
            "image/gif": ".gif",
            "text/markdown": ".md",
            "text/html": ".html",
        }
        ext = ext_map.get(content_type, ".pdf")
        if content_type == "application/pdf" or file_bytes[:4] == b'%PDF':
            ext = ".pdf"

        import hashlib
        file_hash = hashlib.md5(file_bytes).hexdigest()[:8]
        file_name = f"doc_{file_hash}{ext}"

        logger.info(
            f"DocMind analyze_document starting: {len(file_bytes)} bytes, "
            f"filename={file_name}"
        )

        # 1. 提交作业
        job_id = await asyncio.to_thread(self._submit_job, file_bytes, file_name)

        # 2. 轮询状态
        await asyncio.to_thread(self._poll_until_complete, job_id)

        # 3. 获取结果
        result_dict = await asyncio.to_thread(self._get_result, job_id)

        # 4. 解析并映射
        data = result_dict.get("Data", result_dict.get("data", result_dict))
        if not isinstance(data, dict):
            data = {}

        result = _parse_docmind_result(data)

        logger.info(
            f"DocMind analysis complete: "
            f"{len(result.pages)} pages, "
            f"{len(result.paragraphs)} paragraphs, "
            f"{len(result.tables)} tables, "
            f"{len(result.figures)} figures"
        )
        return result

    async def analyze_page(
        self,
        page_image_bytes: bytes,
        page_number: int = 0,
    ) -> dict:
        """
        分析单页图片。

        对单页图片，DocMind 同样走完整的异步提交流程。
        如需性能优化可考虑使用 RecognizeAdvanced 作为单页快捷通道。

        Args:
            page_image_bytes: 单页图片字节流
            page_number: 页码编号

        Returns:
            包含页面结构化信息的 dict (兼容 Azure DI analyze_page 签名)
        """
        result = await self.analyze_document(page_image_bytes, content_type="image/png")

        if not result.paragraphs and not result.tables:
            return {
                "page_number": page_number,
                "width": 0, "height": 0,
                "paragraphs": [], "tables": [], "figures": [],
                "markdown": "", "page_md5": "",
            }

        # 拼接 Markdown
        markdown_lines = [p["content"] for p in result.paragraphs]
        markdown = "\n\n".join(markdown_lines)

        import hashlib
        page_md5 = hashlib.md5(page_image_bytes).hexdigest()

        first_page = result.pages[0] if result.pages else {}

        return {
            "page_number": page_number,
            "width": first_page.get("width", 0),
            "height": first_page.get("height", 0),
            "paragraphs": result.paragraphs,
            "tables": result.tables,
            "figures": result.figures,
            "markdown": markdown,
            "page_md5": page_md5,
        }

    def analyze_document_sync(
        self,
        file_bytes: bytes,
        content_type: str = "application/pdf",
    ) -> DocumentAnalysisResult:
        """同步版文档分析 (兼容性接口)"""
        try:
            loop = asyncio.get_running_loop()
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    asyncio.run,
                    self.analyze_document(file_bytes, content_type),
                )
                return future.result()
        except RuntimeError:
            return asyncio.run(self.analyze_document(file_bytes, content_type))


# ── 工厂函数 ──────────────────────────────────────────────

def create_ocr_client(provider: str = "auto", **kwargs) -> object:
    """
    OCR 客户端工厂 — 根据配置创建 Azure 或阿里云客户端。

    支持三种后端:
      - "azure"  → AzureDocumentIntelligenceClient
      - "aliyun" → AliyunOCRClient (DocMind 大模型版)
      - "auto"   → 自动检测已配置的凭证，优先 Azure (向后兼容)

    Examples:
        client = create_ocr_client()           # 自动检测
        client = create_ocr_client("aliyun")   # DocMind 大模型版
        client = create_ocr_client("azure")    # Azure DI
    """
    if provider == "azure":
        from mineru.backend.rag.azure_doc_intelligence import (
            AzureDocumentIntelligenceClient,
        )
        return AzureDocumentIntelligenceClient(**kwargs)

    if provider == "aliyun":
        return AliyunOCRClient(**kwargs)

    # auto
    aliyun_ak = os.getenv(ALIYUN_AK_ID_ENV)
    azure_key = os.getenv("AZURE_DOC_INTELLIGENCE_KEY")

    if aliyun_ak and not azure_key:
        logger.info("OCR provider auto-detected: aliyun (DocMind)")
        return AliyunOCRClient(**kwargs)
    elif azure_key and not aliyun_ak:
        logger.info("OCR provider auto-detected: azure")
        from mineru.backend.rag.azure_doc_intelligence import (
            AzureDocumentIntelligenceClient,
        )
        return AzureDocumentIntelligenceClient(**kwargs)
    elif aliyun_ak and azure_key:
        logger.info("Both providers configured, defaulting to azure. "
                     "Use create_ocr_client('aliyun') for DocMind.")
        from mineru.backend.rag.azure_doc_intelligence import (
            AzureDocumentIntelligenceClient,
        )
        return AzureDocumentIntelligenceClient(**kwargs)
    else:
        logger.warning("No OCR provider configured. Set credentials in .env file.")
        from mineru.backend.rag.azure_doc_intelligence import (
            AzureDocumentIntelligenceClient,
        )
        return AzureDocumentIntelligenceClient(**kwargs)


# ── __main__ ──────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    async def _main():
        if len(sys.argv) < 2:
            print("Usage: python -m mineru.backend.rag.aliyun_ocr <input_file>")
            sys.exit(1)

        input_path = sys.argv[1]
        with open(input_path, "rb") as f:
            file_bytes = f.read()

        client = AliyunOCRClient()
        print(f"Submitting {input_path} ({len(file_bytes)} bytes)...")
        result = await client.analyze_document(file_bytes)
        print(f"Pages:      {len(result.pages)}")
        print(f"Paragraphs: {len(result.paragraphs)}")
        print(f"Tables:     {len(result.tables)}")
        print(f"Figures:    {len(result.figures)}")
        print(f"Metadata:   {result.metadata}")

        for table in result.tables:
            html = table.get("table_html", "")
            if html:
                print(f"\nTable ({table['row_count']}×{table['col_count']}):")
                print(f"  {html[:300]}...")

    asyncio.run(_main())

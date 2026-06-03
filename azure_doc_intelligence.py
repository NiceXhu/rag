# Copyright (c) Opendatalab. All rights reserved.
"""
Azure Document Intelligence 客户端封装。

使用 Azure Document Intelligence prebuilt-layout 模型进行文档 OCR 和布局分析，
返回结构化的文档分析结果: 文本、表格、图片区域、段落、阅读顺序等。
"""
import asyncio
import os
import time
from dataclasses import dataclass, field
from io import BytesIO
from typing import Optional

# 自动加载项目根目录的 .env 文件 (静默降级)
try:
    from dotenv import load_dotenv, find_dotenv
    _env_path = find_dotenv(usecwd=True)
    if _env_path:
        load_dotenv(_env_path)
except ImportError:
    pass  # python-dotenv 未安装时使用系统环境变量

from loguru import logger

# Azure SDK imports — 需要安装: pip install azure-ai-documentintelligence
from azure.core.credentials import AzureKeyCredential
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import (
    AnalyzeDocumentRequest,
    AnalyzeResult,
    DocumentTable,
    DocumentFigure,
    DocumentParagraph,
    DocumentPage,
    DocumentLine,
    DocumentWord,
    DocumentBarcode,
    DocumentFormula,
)
from azure.core.exceptions import HttpResponseError

from mineru.utils.enum_class import BlockType, ContentType
from mineru.utils.hash_utils import bytes_md5


# ── 配置常量 ──────────────────────────────────────────────
AZURE_DI_ENDPOINT_ENV = "AZURE_DOC_INTELLIGENCE_ENDPOINT"
AZURE_DI_KEY_ENV = "AZURE_DOC_INTELLIGENCE_KEY"
AZURE_DI_API_VERSION = "2024-07-31-preview"
AZURE_DI_MAX_RETRIES = 3
AZURE_DI_RETRY_DELAY = 2.0  # seconds
AZURE_DI_MAX_CONCURRENT_PAGES = 8  # 并发页面分析数


@dataclass
class DocumentAnalysisResult:
    """Azure DI 分析结果的标准化结构"""
    pages: list[dict] = field(default_factory=list)         # 页面级信息
    paragraphs: list[dict] = field(default_factory=list)    # 段落列表
    tables: list[dict] = field(default_factory=list)        # 表格列表
    figures: list[dict] = field(default_factory=list)       # 图片/图表区域列表
    sections: list[dict] = field(default_factory=list)      # 章节结构
    raw_result: Optional[AnalyzeResult] = None              # Azure 原始结果
    metadata: dict = field(default_factory=dict)            # 分析元数据


def _build_page_info(azure_page: DocumentPage) -> dict:
    """将 Azure 页面数据转为标准化的页面信息"""
    lines = []
    if azure_page.lines:
        for line in azure_page.lines:
            words = []
            if line.words:
                for word in line.words:
                    words.append({
                        "content": word.content,
                        "confidence": word.confidence,
                        "polygon": _polygon_to_bbox(word.polygon) if word.polygon else None,
                    })
            lines.append({
                "content": line.content,
                "polygon": _polygon_to_bbox(line.polygon) if line.polygon else None,
                "words": words,
            })

    return {
        "page_number": azure_page.page_number,
        "width": azure_page.width,
        "height": azure_page.height,
        "unit": azure_page.unit,
        "angle": azure_page.angle,
        "lines": lines,
    }


def _build_paragraph_info(para: DocumentParagraph) -> dict:
    """将 Azure 段落数据转为标准化结构"""
    result = {
        "content": para.content,
        "role": para.role,
        "page_number": para.bounding_regions[0].page_number if para.bounding_regions else None,
        "bbox": _polygon_list_to_bbox(para.bounding_regions[0].polygon)
                if para.bounding_regions and para.bounding_regions[0].polygon else None,
    }
    return result


def _build_table_info(table: DocumentTable) -> dict:
    """将 Azure 表格数据转为标准化结构"""
    cells = []
    for cell in table.cells:
        cells.append({
            "row_index": cell.row_index,
            "col_index": cell.column_index,
            "row_span": cell.row_span or 1,
            "col_span": cell.column_span or 1,
            "content": cell.content,
            "kind": cell.kind,
            "page_number": cell.bounding_regions[0].page_number if cell.bounding_regions else None,
            "bbox": _polygon_list_to_bbox(cell.bounding_regions[0].polygon)
                    if cell.bounding_regions and cell.bounding_regions[0].polygon else None,
        })

    page_numbers = set()
    for region in (table.bounding_regions or []):
        page_numbers.add(region.page_number)

    return {
        "row_count": table.row_count,
        "col_count": table.column_count,
        "cells": cells,
        "page_numbers": sorted(page_numbers),
        "caption": _extract_caption(table.caption) if hasattr(table, 'caption') and table.caption else None,
        "footnotes": [_extract_caption(fn) for fn in table.footnotes] if hasattr(table, 'footnotes') and table.footnotes else [],
        "table_html": _cells_to_html(cells, table.row_count, table.column_count),
    }


def _build_figure_info(figure: DocumentFigure) -> dict:
    """将 Azure 图片/图表区域转为标准化结构"""
    page_numbers = set()
    for region in (figure.bounding_regions or []):
        page_numbers.add(region.page_number)

    return {
        "id": figure.id,
        "page_numbers": sorted(page_numbers),
        "bbox": _polygon_list_to_bbox(figure.bounding_regions[0].polygon)
                if figure.bounding_regions and figure.bounding_regions[0].polygon else None,
        "caption": _extract_caption(figure.caption) if hasattr(figure, 'caption') and figure.caption else None,
        "footnotes": [_extract_caption(fn) for fn in figure.footnotes] if hasattr(figure, 'footnotes') and figure.footnotes else [],
    }


def _extract_caption(caption_obj) -> str:
    """从 Azure DI caption 对象中提取文本"""
    if caption_obj is None:
        return ""
    if hasattr(caption_obj, 'content'):
        return caption_obj.content
    return str(caption_obj)


def _polygon_to_bbox(polygon) -> list[int]:
    """将 Azure 的多边形顶点转为 [x0, y0, x1, y1] 的 bbox"""
    if not polygon:
        return [0, 0, 0, 0]
    xs = [p.get("x", p[0]) if isinstance(p, dict) else p[0] for p in polygon]
    ys = [p.get("y", p[1]) if isinstance(p, dict) else p[1] for p in polygon]
    return [int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))]


def _polygon_list_to_bbox(polygon) -> list[int]:
    """将 polygon 点列表转为 bbox"""
    return _polygon_to_bbox(polygon)


def _detect_header_hierarchy(cells: list[dict]) -> dict:
    """
    检测多层表头的父子关系。

    基于 Azure DI 的 cell.kind + col_span/row_span 判断:
    - colspan > 1 → 父级表头 (scope="colgroup")
    - colspan == 1 → 叶级表头 (scope="col")
    - rowspan > 1 且 colspan > 1 → 跨行列父级

    Returns:
        {(row_idx, col_idx): "colgroup" | "col" | "row" | "rowgroup"}
    """
    hierarchy = {}
    for cell in cells:
        if cell.get("kind") != "columnHeader":
            continue
        r, c = cell.get("row_index", 0), cell.get("col_index", 0)
        cs = cell.get("col_span", 1)
        rs = cell.get("row_span", 1)

        if cs > 1:
            hierarchy[(r, c)] = "colgroup"  # 父级: 跨多列
        else:
            hierarchy[(r, c)] = "col"        # 叶级: 单列
    return hierarchy


def _cells_to_html(cells: list[dict], row_count: int, col_count: int) -> str:
    """将 Azure 表格单元格列表转为 HTML 表格 (支持多层表头)"""
    # 构建行×列的二维数组
    grid = [[None for _ in range(col_count)] for _ in range(row_count)]
    for cell in cells:
        r, c = cell["row_index"], cell["col_index"]
        if r < row_count and c < col_count:
            grid[r][c] = cell

    # ★ 检测多层表头结构
    hierarchy = _detect_header_hierarchy(cells)
    header_rows = set(r for (r, c) in hierarchy.keys())

    # 生成 HTML
    html_parts = ["<table>"]

    # ── thead: 所有表头行 ──
    if header_rows:
        html_parts.append("<thead>")
        for row_idx in range(row_count):
            if row_idx not in header_rows:
                continue
            html_parts.append("<tr>")
            for col_idx in range(col_count):
                cell = grid[row_idx][col_idx]
                if cell is None:
                    continue
                if cell["row_index"] == row_idx and cell["col_index"] == col_idx:
                    attrs = []
                    if cell.get("col_span", 1) > 1:
                        attrs.append(f'colspan="{cell["col_span"]}"')
                    if cell.get("row_span", 1) > 1:
                        attrs.append(f'rowspan="{cell["row_span"]}"')
                    # ★ scope 属性标记父子关系
                    scope = hierarchy.get((row_idx, col_idx), "col")
                    attrs.append(f'scope="{scope}"')
                    attr_str = " " + " ".join(attrs)
                    content = cell.get("content", "").replace("\n", "<br>")
                    html_parts.append(f"<th{attr_str}>{content}</th>")
            html_parts.append("</tr>")
        html_parts.append("</thead>")

    # ── tbody: 数据行 ──
    html_parts.append("<tbody>")
    for row_idx in range(row_count):
        if row_idx in header_rows:
            continue
        html_parts.append("<tr>")
        for col_idx in range(col_count):
            cell = grid[row_idx][col_idx]
            if cell is None:
                continue
            if cell["row_index"] == row_idx and cell["col_index"] == col_idx:
                attrs = []
                if cell.get("col_span", 1) > 1:
                    attrs.append(f'colspan="{cell["col_span"]}"')
                if cell.get("row_span", 1) > 1:
                    attrs.append(f'rowspan="{cell["row_span"]}"')
                tag = "th" if cell.get("kind") == "rowHeader" else "td"
                attr_str = " " + " ".join(attrs) if attrs else ""
                content = cell.get("content", "").replace("\n", "<br>")
                html_parts.append(f"<{tag}{attr_str}>{content}</{tag}>")
        html_parts.append("</tr>")
    html_parts.append("</tbody>")
    html_parts.append("</table>")
    return "\n".join(html_parts)


class AzureDocumentIntelligenceClient:
    """Azure Document Intelligence 客户端 (单例模式)"""

    _instance: Optional["AzureDocumentIntelligenceClient"] = None
    _lock = __import__('threading').RLock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, endpoint: Optional[str] = None, key: Optional[str] = None):
        with self._lock:
            if hasattr(self, '_initialized') and self._initialized:
                return
            self._endpoint = endpoint or os.getenv(AZURE_DI_ENDPOINT_ENV, "")
            self._key = key or os.getenv(AZURE_DI_KEY_ENV, "")
            self._client: Optional[DocumentIntelligenceClient] = None
            self._semaphore = asyncio.Semaphore(AZURE_DI_MAX_CONCURRENT_PAGES)
            self._initialized = True

    def _get_client(self) -> DocumentIntelligenceClient:
        """获取或延迟初始化 Azure DI Client"""
        if self._client is None:
            if not self._endpoint or not self._key:
                raise RuntimeError(
                    f"Azure Document Intelligence credentials not configured. "
                    f"Set {AZURE_DI_ENDPOINT_ENV} and {AZURE_DI_KEY_ENV} env vars."
                )
            self._client = DocumentIntelligenceClient(
                endpoint=self._endpoint,
                credential=AzureKeyCredential(self._key),
                api_version=AZURE_DI_API_VERSION,
            )
        return self._client

    async def analyze_document(
        self,
        file_bytes: bytes,
        content_type: str = "application/pdf",
    ) -> DocumentAnalysisResult:
        """分析文档，返回结构化结果"""
        client = self._get_client()
        result = DocumentAnalysisResult()

        for attempt in range(AZURE_DI_MAX_RETRIES):
            try:
                async with self._semaphore:
                    poller = await client.begin_analyze_document(
                        model_id="prebuilt-layout",
                        analyze_request=AnalyzeDocumentRequest(
                            bytes_source=file_bytes,
                        ),
                        content_type=content_type,
                        output_content_format="markdown",
                    )
                    azure_result: AnalyzeResult = await poller.result()

                result.raw_result = azure_result
                result.metadata = {
                    "model_id": azure_result.model_id,
                    "api_version": azure_result.api_version,
                    "content_format": azure_result.content_format,
                    "page_count": len(azure_result.pages) if azure_result.pages else 0,
                }

                # 转换页面信息
                if azure_result.pages:
                    result.pages = [_build_page_info(p) for p in azure_result.pages]

                # 转换段落
                if azure_result.paragraphs:
                    result.paragraphs = [_build_paragraph_info(p) for p in azure_result.paragraphs]

                # 转换表格
                if azure_result.tables:
                    result.tables = [_build_table_info(t) for t in azure_result.tables]

                # 转换图片/图表区域
                if azure_result.figures:
                    result.figures = [_build_figure_info(f) for f in azure_result.figures]

                # 提取章节结构
                if azure_result.sections:
                    result.sections = [
                        {
                            "element_count": len(s.elements) if s.elements else 0,
                            "paragraph_count": sum(1 for e in (s.elements or []) if e.startswith("/paragraphs/")),
                            "table_count": sum(1 for e in (s.elements or []) if e.startswith("/tables/")),
                            "figure_count": sum(1 for e in (s.elements or []) if e.startswith("/figures/")),
                        }
                        for s in azure_result.sections
                    ]

                logger.info(
                    f"Azure DI analysis complete: "
                    f"{len(result.pages)} pages, "
                    f"{len(result.paragraphs)} paragraphs, "
                    f"{len(result.tables)} tables, "
                    f"{len(result.figures)} figures"
                )
                return result

            except HttpResponseError as e:
                logger.warning(f"Azure DI request failed (attempt {attempt + 1}/{AZURE_DI_MAX_RETRIES}): {e}")
                if attempt < AZURE_DI_MAX_RETRIES - 1:
                    await asyncio.sleep(AZURE_DI_RETRY_DELAY * (attempt + 1))
                else:
                    raise RuntimeError(f"Azure DI analysis failed after {AZURE_DI_MAX_RETRIES} attempts: {e}") from e

    async def analyze_page(
        self,
        page_image_bytes: bytes,
        page_number: int = 0,
    ) -> dict:
        """分析单页图片"""
        client = self._get_client()

        for attempt in range(AZURE_DI_MAX_RETRIES):
            try:
                async with self._semaphore:
                    poller = await client.begin_analyze_document(
                        model_id="prebuilt-layout",
                        analyze_request=AnalyzeDocumentRequest(
                            bytes_source=page_image_bytes,
                        ),
                        content_type="image/jpeg",
                        output_content_format="markdown",
                    )
                    azure_result = await poller.result()

                page_info = {
                    "page_number": page_number,
                    "width": azure_result.pages[0].width if azure_result.pages else 0,
                    "height": azure_result.pages[0].height if azure_result.pages else 0,
                    "paragraphs": [_build_paragraph_info(p) for p in (azure_result.paragraphs or [])],
                    "tables": [_build_table_info(t) for t in (azure_result.tables or [])],
                    "figures": [_build_figure_info(f) for f in (azure_result.figures or [])],
                    "markdown": azure_result.content if azure_result.content else "",
                    "page_md5": bytes_md5(page_image_bytes),
                }
                return page_info

            except HttpResponseError as e:
                logger.warning(f"Azure DI page analysis failed (attempt {attempt + 1}): {e}")
                if attempt < AZURE_DI_MAX_RETRIES - 1:
                    await asyncio.sleep(AZURE_DI_RETRY_DELAY * (attempt + 1))
                else:
                    raise RuntimeError(f"Page analysis failed: {e}") from e

    def analyze_document_sync(
        self,
        file_bytes: bytes,
        content_type: str = "application/pdf",
    ) -> DocumentAnalysisResult:
        """同步版文档分析 (兼容性接口)"""
        import asyncio as _asyncio

        try:
            loop = _asyncio.get_running_loop()
            # 已在事件循环中 — 在新线程的事件循环中执行
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    _asyncio.run,
                    self.analyze_document(file_bytes, content_type),
                )
                return future.result()
        except RuntimeError:
            # 没有运行中的事件循环
            return _asyncio.run(self.analyze_document(file_bytes, content_type))

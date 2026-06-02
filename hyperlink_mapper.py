# Copyright (c) Opendatalab. All rights reserved.
"""
PDF 超链接提取与回插。

从原始 PDF 提取超链接 (pypdfium2 annotations), 按位置匹配到
middle_json 的 span 级别, 在 Markdown 生成时回插为 [text](url)。

处理要点:
1. 坐标对齐: pypdfium2 返回 PDF points (1/72 inch), Azure DI 返回 inches
2. 表格去重: 同一 cell 可能跨多行, 超链接只插入一次
3. 位置精确匹配: 基于 bbox IoU + 包含关系
"""
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import pypdfium2 as pdfium
from loguru import logger

from mineru.utils.enum_class import BlockType, ContentType
from mineru.utils.pdfium_guard import (
    close_pdfium_document,
    get_pdfium_document_page_count,
    open_pdfium_document,
    pdfium_guard,
)

# ── 常量 ──────────────────────────────────────────────────
# bbox 重叠阈值: IoU 或包含率超过此值认为匹配
OVERLAP_THRESHOLD = 0.3
# 多 span 匹配时的最大距离 (inches)
MAX_SPAN_MERGE_DISTANCE = 0.5


@dataclass
class PdfHyperlink:
    """PDF 超链接"""
    uri: str
    page: int          # 0-based
    bbox: list[float]  # [x0, y0, x1, y1] in inches
    rect_raw: list[float] = field(default_factory=list)  # 原始 PDF points


# ── 提取 ──────────────────────────────────────────────────

def extract_pdf_links(pdf_bytes: bytes) -> list[PdfHyperlink]:
    """
    从 PDF 字节流中提取所有超链接。

    使用 pypdfium2 读取每页的 link annotations,
    将坐标从 PDF points 转为 inches。

    Returns:
        PdfHyperlink 列表 (包含 page, bbox, uri)
    """
    links: list[PdfHyperlink] = []

    pdf_doc = None
    try:
        with pdfium_guard():
            pdf_doc = pdfium.PdfDocument(pdf_bytes)

        for page_idx in range(len(pdf_doc)):
            with pdfium_guard():
                page = pdf_doc[page_idx]

                # 获取页面尺寸用于坐标转换
                page_size = page.get_size()  # (width, height) in points

                # 提取链接 — pypdfium2 的 get_links 方法
                try:
                    page_links = page.get_links()
                except (AttributeError, TypeError):
                    # 旧版本 pypdfium2 或不支持
                    continue

                for link_info in page_links:
                    uri = link_info.get("uri", "")
                    if not uri:
                        continue

                    # 获取位置 — 可能是 "pos" 或 "rect"
                    rect = link_info.get("pos") or link_info.get("rect") or []
                    if len(rect) < 4:
                        continue

                    # PDF points → inches (1 point = 1/72 inch)
                    x0, y0, x1, y1 = rect[:4]
                    bbox_inches = [
                        x0 / 72.0,
                        y0 / 72.0,
                        x1 / 72.0,
                        y1 / 72.0,
                    ]

                    links.append(PdfHyperlink(
                        uri=uri,
                        page=page_idx,
                        bbox=bbox_inches,
                        rect_raw=list(rect[:4]),
                    ))

        logger.info(f"PDF 超链接提取: {len(links)} 个链接, {len(set(l.page for l in links))} 页")

    except Exception as e:
        logger.warning(f"PDF 超链接提取失败: {e}")
    finally:
        if pdf_doc is not None:
            close_pdfium_document(pdf_doc)

    return links


# ── 匹配 ──────────────────────────────────────────────────

def _bbox_overlap_ratio(bbox_a: list[float], bbox_b: list[float]) -> float:
    """
    计算两个 bbox 的重叠率 (IoU)。

    坐标体系统一为 inches。
    """
    ax0, ay0, ax1, ay1 = bbox_a
    bx0, by0, bx1, by1 = bbox_b

    # 交集
    ix0 = max(ax0, bx0)
    iy0 = max(ay0, by0)
    ix1 = min(ax1, bx1)
    iy1 = min(ay1, by1)

    if ix0 >= ix1 or iy0 >= iy1:
        return 0.0

    inter_area = (ix1 - ix0) * (iy1 - iy0)
    area_a = max((ax1 - ax0) * (ay1 - ay0), 0.0001)
    area_b = max((bx1 - bx0) * (by1 - by0), 0.0001)

    return inter_area / min(area_a, area_b)  # 用较小面积归一化


def _span_center(span: dict) -> tuple[float, float]:
    """获取 span 的 bbox 中心点"""
    bbox = span.get("bbox") or [0, 0, 0, 0]
    return ((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)


def _span_has_link(span: dict) -> bool:
    """检查 span 是否已有超链接标记"""
    return bool(span.get("_hyperlink"))


def _iter_all_spans(middle_json: dict):
    """遍历 middle_json 中的所有 span"""
    for page_info in middle_json.get("pdf_info", []):
        page_idx = page_info.get("page_idx", 0)
        for block in page_info.get("preproc_blocks", []):
            yield from _iter_block_spans(block, page_idx)


def _iter_block_spans(block: dict, page_idx: int):
    """递归遍历 block 中的所有 span (包括嵌套的 list/table blocks)"""
    for line in block.get("lines", []):
        for span in line.get("spans", []):
            yield span, block, page_idx

    for sub_block in block.get("blocks", []):
        yield from _iter_block_spans(sub_block, page_idx)


def map_links_to_middle_json(
    middle_json: dict,
    pdf_links: list[PdfHyperlink],
) -> dict:
    """
    将 PDF 超链接匹配到 middle_json 的 span 上。

    匹配逻辑:
    1. 按页分组超链接
    2. 对每个 span, 找与其 bbox 重叠度最高的链接
    3. 重叠率 > OVERLAP_THRESHOLD → 标记 span._hyperlink = uri
    4. 同一链接匹配到多个 span 时, 仅保留重叠率最高的那个 (表格去重)

    Returns:
        修改后的 middle_json (就地修改)
    """
    if not pdf_links:
        return middle_json

    # 按页索引超链接
    links_by_page: dict[int, list[PdfHyperlink]] = defaultdict(list)
    for link in pdf_links:
        links_by_page[link.page].append(link)

    # 统计
    total_matched = 0

    # 第一遍: 收集每个链接的所有候选 span
    # (link_page, link_bbox_tuple) → [(overlap, span, link_uri)]
    link_candidates: dict[tuple, list[tuple[float, dict, str]]] = defaultdict(list)

    for span, block, page_idx in _iter_all_spans(middle_json):
        span_type = span.get("type", "")
        if span_type not in (ContentType.TEXT, ContentType.INLINE_EQUATION):
            continue

        span_bbox = span.get("bbox")
        if not span_bbox or len(span_bbox) < 4:
            continue

        page_links = links_by_page.get(page_idx, [])
        for link in page_links:
            overlap = _bbox_overlap_ratio(span_bbox, link.bbox)
            if overlap > OVERLAP_THRESHOLD:
                # 用 (page, bbox_tuple) 作为去重 key, 避免 URI 中 ':' 的解析问题
                link_key = (link.page, tuple(round(v, 1) for v in link.bbox))
                link_candidates[link_key].append((overlap, span, link.uri))

    # 第二遍: 每个链接只标记最佳匹配的 span
    for link_key, candidates in link_candidates.items():
        if not candidates:
            continue

        candidates.sort(key=lambda x: x[0], reverse=True)
        _, best_span, best_uri = candidates[0]

        # 检查是否有多个高重叠 span (表格跨行场景)
        high_overlap = [
            (o, s, u) for o, s, u in candidates
            if o > OVERLAP_THRESHOLD + 0.2
        ]

        if len(high_overlap) > 1:
            # 表格 cell 跨行: 合并文本, 标记其余 span 为 merged
            merged_parts = []
            for _, s, _ in high_overlap:
                txt = s.get("content", "").strip()
                if txt:
                    merged_parts.append(txt)

            if merged_parts:
                merged = " ".join(merged_parts)
                if merged != best_span.get("content", "").strip():
                    best_span["content"] = merged

            # 标记其余 span 跳过输出
            for _, other_span, _ in high_overlap[1:]:
                other_span["_hyperlink_merged"] = True

        # 标记最佳 span
        if not best_span.get("_hyperlink"):
            best_span["_hyperlink"] = best_uri
            total_matched += 1

    logger.info(
        f"超链接匹配: {len(pdf_links)} links → {total_matched} spans 标记"
    )
    return middle_json


# ── 便捷入口 ──────────────────────────────────────────────

def apply_hyperlinks_to_middle_json(
    middle_json: dict,
    pdf_bytes: bytes,
) -> dict:
    """
    一键提取 PDF 超链接并匹配到 middle_json。

    Args:
        middle_json: 已构建的 middle_json
        pdf_bytes: 原始 PDF 字节流

    Returns:
        修改后的 middle_json
    """
    links = extract_pdf_links(pdf_bytes)
    if not links:
        return middle_json
    return map_links_to_middle_json(middle_json, links)

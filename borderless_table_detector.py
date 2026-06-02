# Copyright (c) Opendatalab. All rights reserved.
"""
无框线表格检测器 (Borderless Table Detector)。

场景: PPT 转 PDF、Word 无边框表格、或其他无可见网格线的表格。
Azure DI 可能将其归类为普通文本段落, 需要用启发式方法重新检测。

检测原理:
1. 扫描页面上的文本块 (paragraphs)
2. 按 y 坐标聚类为「行」
3. 按 x 坐标聚类为「列」
4. 如果行列对齐度超过阈值 → 判定为无框线表格
5. 提取单元格内容, 生成标准 table dict (兼容 Azure DI 格式)
6. 用检测到的表格替换原始文本块
"""
from collections import defaultdict
from typing import Optional

from loguru import logger


# ── 阈值配置 ──────────────────────────────────────────────
MIN_ROWS = 2                    # 最少行数
MIN_COLS = 2                    # 最少列数
ROW_Y_TOLERANCE = 0.15          # 同行文本 y 坐标最大偏差 (inches)
COL_X_TOLERANCE = 0.3           # 同列文本 x 坐标最大偏差 (inches)
COLUMN_ALIGNMENT_RATIO = 0.7    # 列对齐率: 每行中与模板列对齐的文本比例
TABLE_MIN_COVERAGE = 0.3        # 检测到的表格至少覆盖页面的比例


def _parse_bbox(bbox) -> Optional[list[float]]:
    """安全解析 bbox"""
    if not bbox or len(bbox) < 4:
        return None
    return [float(v) for v in bbox[:4]]


def _mid_y(bbox: list[float]) -> float:
    """bbox 中心 y 坐标"""
    return (bbox[1] + bbox[3]) / 2


def _mid_x(bbox: list[float]) -> float:
    """bbox 中心 x 坐标"""
    return (bbox[0] + bbox[2]) / 2


def _cluster_1d(values: list[float], tolerance: float) -> list[tuple[float, list[int]]]:
    """
    一维聚类: 将相近的值归为一组。

    Returns:
        [(center, [indices]), ...] 按 center 升序排列
    """
    if not values:
        return []

    # 排序
    sorted_pairs = sorted(enumerate(values), key=lambda x: x[1])
    clusters = []
    current_vals = [sorted_pairs[0][1]]
    current_indices = [sorted_pairs[0][0]]

    for idx, val in sorted_pairs[1:]:
        if val - current_vals[-1] <= tolerance:
            current_vals.append(val)
            current_indices.append(idx)
        else:
            center = sum(current_vals) / len(current_vals)
            clusters.append((center, current_indices))
            current_vals = [val]
            current_indices = [idx]

    if current_vals:
        center = sum(current_vals) / len(current_vals)
        clusters.append((center, current_indices))

    # 按 center 排序
    clusters.sort(key=lambda x: x[0])
    return clusters


def detect_borderless_table(
    paragraphs: list[dict],
    page_width: float = 0,
    page_height: float = 0,
) -> Optional[dict]:
    """
    检测无框线表格。

    当 Azure DI 未检测到表格, 但段落排列呈现规则网格时触发。

    Args:
        paragraphs: 页面上的段落列表 (含 content + bbox)
        page_width: 页面宽度 (inches)
        page_height: 页面高度 (inches)

    Returns:
        标准化 table dict 或 None (未检测到表格)
    """
    if len(paragraphs) < MIN_ROWS * MIN_COLS:
        return None

    # 提取有效段落 (有 bbox 和有内容的)
    valid_paras = []
    for p in paragraphs:
        bbox = _parse_bbox(p.get("bbox"))
        content = (p.get("content") or "").strip()
        if bbox and content and len(content) > 1:
            valid_paras.append({"bbox": bbox, "content": content, "_orig": p})

    if len(valid_paras) < MIN_ROWS * MIN_COLS:
        return None

    # ── 1. 按 y 聚类为行 ───────────────────────────
    y_values = [_mid_y(p["bbox"]) for p in valid_paras]
    row_clusters = _cluster_1d(y_values, ROW_Y_TOLERANCE)

    # 过滤: 行内至少有 MIN_COLS 个元素
    rows = []
    for y_center, indices in row_clusters:
        if len(indices) >= MIN_COLS:
            rows.append((y_center, indices))

    if len(rows) < MIN_ROWS:
        return None

    # ── 2. 为每行内部按 x 聚类为列 ────────────────
    #  先收集所有行的 x 聚类中心, 构建全局列模板
    all_row_x_centers = []
    row_cells = []  # [(row_idx, x_center, para_idx)]

    for row_idx, (y_center, indices) in enumerate(rows):
        x_values = [_mid_x(valid_paras[i]["bbox"]) for i in indices]
        x_clusters = _cluster_1d(x_values, COL_X_TOLERANCE)

        row_x_centers = []
        for x_center, x_sub_indices in x_clusters:
            # x_sub_indices 是 x_values 中的索引, 映射回 paragraphs
            for sub_idx in x_sub_indices:
                para_idx = indices[sub_idx]
                row_cells.append((row_idx, x_center, para_idx))
            row_x_centers.append(x_center)

        row_x_centers.sort()
        all_row_x_centers.append(row_x_centers)

    # ── 3. 建立全局列模板 ──────────────────────────
    #  取所有 x_center 进行二次聚类作为列定义
    all_x_vals = [x for row_x in all_row_x_centers for x in row_x]
    col_template = _cluster_1d(all_x_vals, COL_X_TOLERANCE)
    col_centers = [c for c, _ in col_template]

    if len(col_centers) < MIN_COLS:
        return None

    # ── 4. 对齐检查: 每行有多少元素与列模板对齐 ────
    aligned_count = 0
    total_row_elements = 0

    for row_idx, (y_center, indices) in enumerate(rows):
        x_values = [_mid_x(valid_paras[i]["bbox"]) for i in indices]
        total_row_elements += len(x_values)

        # 对每个 x 值, 找最近的列中心
        for x in x_values:
            nearest_col = min(col_centers, key=lambda c: abs(c - x))
            if abs(nearest_col - x) < COL_X_TOLERANCE:
                aligned_count += 1

    alignment_ratio = aligned_count / max(total_row_elements, 1)

    if alignment_ratio < COLUMN_ALIGNMENT_RATIO:
        logger.debug(
            f"无框线表格检测: 对齐率 {alignment_ratio:.2f} < {COLUMN_ALIGNMENT_RATIO}, 跳过"
        )
        return None

    # ── 5. 构建 cell 网格 ──────────────────────────
    #  rows × cols, 每个格子可能为空
    num_cols = len(col_centers)
    grid: list[list[list[dict]]] = [
        [[] for _ in range(num_cols)]
        for _ in range(len(rows))
    ]

    used_indices: set[int] = set()

    for row_idx, (y_center, indices) in enumerate(rows):
        for para_idx in indices:
            if para_idx in used_indices:
                continue

            para = valid_paras[para_idx]
            x = _mid_x(para["bbox"])

            # 找最近的列
            col_idx = min(range(num_cols), key=lambda c: abs(col_centers[c] - x))
            if abs(col_centers[col_idx] - x) < COL_X_TOLERANCE:
                grid[row_idx][col_idx].append(para)
                used_indices.add(para_idx)

    # ── 6. 生成标准化 table dict ───────────────────
    cells = []
    for row_idx in range(len(rows)):
        for col_idx in range(num_cols):
            cell_paras = grid[row_idx][col_idx]
            if not cell_paras:
                continue

            # 合并同一格子的多个段落
            content = " ".join(p["content"] for p in cell_paras)

            # 计算合并后的 bbox
            bboxes = [p["bbox"] for p in cell_paras]
            merged_bbox = [
                min(b[0] for b in bboxes),
                min(b[1] for b in bboxes),
                max(b[2] for b in bboxes),
                max(b[3] for b in bboxes),
            ]

            cells.append({
                "row_index": row_idx,
                "col_index": col_idx,
                "row_span": 1,
                "col_span": 1,
                "content": content,
                "kind": "",  # 待后续检测确定
                "bbox": merged_bbox,
            })

    # ── 7. 检测跨行/跨列合并 ───────────────────────
    cells = _detect_merged_cells(cells, col_centers, rows)

    # ★ 多层表头检测 (在合并检测之后, 利用 col_span 信息)
    _detect_multi_level_headers(cells, len(rows))

    # ── 8. 生成 HTML ──────────────────────────────
    table_html = _grid_to_html(cells, len(rows), num_cols)

    # 计算整体 bbox
    all_cell_bboxes = [c["bbox"] for c in cells if c.get("bbox")]
    table_bbox = [
        min(b[0] for b in all_cell_bboxes),
        min(b[1] for b in all_cell_bboxes),
        max(b[2] for b in all_cell_bboxes),
        max(b[3] for b in all_cell_bboxes),
    ] if all_cell_bboxes else [0, 0, 0, 0]

    logger.info(
        f"无框线表格检测: {len(rows)} 行 × {num_cols} 列, "
        f"{len(cells)} cells, 对齐率 {alignment_ratio:.2f}"
    )

    return {
        "row_count": len(rows),
        "col_count": num_cols,
        "cells": cells,
        "page_numbers": [],  # 由调用方填充
        "caption": None,
        "footnotes": [],
        "table_html": table_html,
        "_detected_borderless": True,
        "_alignment_ratio": alignment_ratio,
        "_bbox": table_bbox,
    }


def _detect_merged_cells(
    cells: list[dict],
    col_centers: list[float],
    rows: list,
) -> list[dict]:
    """
    检测跨行/跨列合并的单元格。

    基于 bbox 的覆盖范围:
    - 如果 cell bbox x 范围覆盖多个列中心 → colspan
    - 如果 cell bbox y 范围覆盖多个行中心 → rowspan
    """
    if not cells or not col_centers or not rows:
        return cells

    row_y_centers = [y for y, _ in rows]

    for cell in cells:
        bbox = cell.get("bbox")
        if not bbox or len(bbox) < 4:
            continue

        # 检测 colspan
        covered_cols = 0
        for cc in col_centers:
            if bbox[0] - COL_X_TOLERANCE <= cc <= bbox[2] + COL_X_TOLERANCE:
                covered_cols += 1
        if covered_cols > 1:
            cell["col_span"] = covered_cols

        # 检测 rowspan
        covered_rows = 0
        for ry in row_y_centers:
            if bbox[1] - ROW_Y_TOLERANCE <= ry <= bbox[3] + ROW_Y_TOLERANCE:
                covered_rows += 1
        if covered_rows > 1:
            cell["row_span"] = covered_rows

    return cells


def _detect_multi_level_headers(cells: list[dict], row_count: int) -> None:
    """
    多层表头检测 (在 merged cells 检测之后调用)。

    利用 col_span 和文本长度判断哪些行是表头:
    - Row 0: 始终是表头
    - Row 1+: 如果有 col_span > 1 (父级表头) 或文本显著短于数据行 → 表头
    """
    if row_count <= 2:
        # ≤ 2 行的表: 只有 row 0 是表头
        for cell in cells:
            if cell["row_index"] == 0:
                cell["kind"] = "columnHeader"
        return

    # Row 0 始终是表头
    for cell in cells:
        if cell["row_index"] == 0:
            cell["kind"] = "columnHeader"

    # 计算数据行的平均文本长度 (跳过 row 0 和 row 1)
    data_rows = [r for r in range(2, row_count)]
    data_lengths = []
    for cell in cells:
        if cell["row_index"] in data_rows:
            data_lengths.append(len(cell.get("content", "") or ""))
    avg_data_len = sum(data_lengths) / max(len(data_lengths), 1)

    # 检查 row 1 是否为表头
    row1_cells = [c for c in cells if c["row_index"] == 1]
    if row1_cells:
        row1_has_colspan = any((c.get("col_span") or 1) > 1 for c in row1_cells)
        row1_avg_len = sum(len(c.get("content", "") or "") for c in row1_cells) / len(row1_cells)

        # 判定: 有跨列合并 或 文本显著短于数据行 → 表头
        is_header = (
            row1_has_colspan or
            (avg_data_len > 0 and row1_avg_len < avg_data_len * 0.6)
        )
        if is_header:
            for cell in row1_cells:
                cell["kind"] = "columnHeader"


def _grid_to_html(cells: list[dict], row_count: int, col_count: int) -> str:
    """将 cell 列表渲染为 HTML 表格"""
    # 构建网格
    grid = [[None for _ in range(col_count)] for _ in range(row_count)]
    for cell in cells:
        r, c = cell.get("row_index", 0), cell.get("col_index", 0)
        if 0 <= r < row_count and 0 <= c < col_count:
            grid[r][c] = cell

    html_parts = ["<table>"]

    # 检测表头行
    header_rows = set()
    for cell in cells:
        if cell.get("kind") == "columnHeader":
            header_rows.add(cell["row_index"])

    if header_rows:
        html_parts.append("<thead>")
        for r in sorted(header_rows):
            html_parts.append(_render_grid_row(grid, r, col_count, "th", is_header=True))
        html_parts.append("</thead>")

    html_parts.append("<tbody>")
    for r in range(row_count):
        if r not in header_rows:
            html_parts.append(_render_grid_row(grid, r, col_count, "td", is_header=False))
    html_parts.append("</tbody>")
    html_parts.append("</table>")

    return "\n".join(html_parts)


def _render_grid_row(grid: list[list], row_idx: int, col_count: int, tag: str,
                     is_header: bool = False) -> str:
    """渲染网格的一行"""
    parts = ["<tr>"]
    col = 0
    while col < col_count:
        cell = grid[row_idx][col] if row_idx < len(grid) else None
        if cell is None:
            col += 1
            continue

        if cell.get("row_index") == row_idx and cell.get("col_index") == col:
            attrs = []
            rs = cell.get("row_span") or 1
            cs = cell.get("col_span") or 1
            if rs > 1:
                attrs.append(f'rowspan="{rs}"')
            if cs > 1:
                attrs.append(f'colspan="{cs}"')
            # ★ 多层表头 scope
            if is_header:
                scope = "colgroup" if cs > 1 else "col"
                attrs.append(f'scope="{scope}"')
            attr_str = " " + " ".join(attrs) if attrs else ""
            content = (cell.get("content") or "").replace("\n", "<br>")
            parts.append(f"<{tag}{attr_str}>{content}</{tag}>")
            col += cs
        else:
            col += 1

    parts.append("</tr>")
    return "\n".join(parts)


# ── 集成入口 ──────────────────────────────────────────────

def detect_tables_in_page(
    paragraphs: list[dict],
    existing_tables: list[dict],
    page_width: float = 0,
    page_height: float = 0,
) -> list[dict]:
    """
    对单页进行无框线表格补检。

    如果 Azure DI 未检测到表格, 但段落布局呈现网格特征, 则生成表格。

    Args:
        paragraphs: 页面段落
        existing_tables: Azure DI 已检测到的表格
        page_width: 页面宽度
        page_height: 页面高度

    Returns:
        增强后的表格列表 (原始表格 + 新检测到的表格)
    """
    result = list(existing_tables)

    # 如果已有表格覆盖了大部分页面, 不再补检
    if existing_tables:
        return result

    # 检测
    detected = detect_borderless_table(paragraphs, page_width, page_height)

    if detected:
        # 将检测到的表格内的段落从 paragraphs 中标记为「已处理」
        # (在实际使用中, 调用方负责从段落列表中移除这些段落)
        result.append(detected)

    return result


def apply_borderless_detection(pages_data: list[dict]) -> list[dict]:
    """
    对所有页面进行无框线表格补检。

    Args:
        pages_data: 页面数据列表

    Returns:
        修改后的 pages_data (就地修改)
    """
    total_detected = 0

    for page in pages_data:
        existing = page.get("tables", [])
        paragraphs = page.get("paragraphs", [])

        if not paragraphs:
            continue

        enhanced = detect_tables_in_page(
            paragraphs=paragraphs,
            existing_tables=existing,
            page_width=page.get("width", 0),
            page_height=page.get("height", 0),
        )

        if len(enhanced) > len(existing):
            page["tables"] = enhanced
            total_detected += 1

    if total_detected:
        logger.info(f"无框线表格补检: {total_detected} 页检测到新表格")

    return pages_data

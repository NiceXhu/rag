# Copyright (c) Opendatalab. All rights reserved.
"""
跨页表格处理 — 表头检测、去重、合并。

处理场景:
1. 同一个表格被 Azure DI 识别为单个多页表格 (page_numbers 含多页)
2. 同一个表格被 Azure DI 识别为多个独立表格 (每个 page_numbers 仅 1 页)

核心逻辑:
- 利用 Azure DI cells 的 `kind` 字段 ("columnHeader") 识别表头行
- 对跨页表格的续页部分: 移除重复的表头行
- 合并为一个逻辑表格, 输出正确的 HTML
"""
import re
from collections import defaultdict
from typing import Optional

from loguru import logger


# ── 配置 ──────────────────────────────────────────────────
# 表头相似度阈值: 续页表头与首页表头内容的 fuzzy 匹配阈值
HEADER_SIMILARITY_THRESHOLD = 0.7
# 续页表格起始位置阈值: 表格顶部 y 坐标 < 此值 (inch) 视为页面顶部表格
CONTINUATION_Y_THRESHOLD = 1.5  # inches from page top


def _get_cell_text(cell: dict) -> str:
    """获取 cell 的纯文本内容"""
    return (cell.get("content") or "").strip()


def _row_index(cell: dict) -> int:
    return cell.get("row_index", 0)


def _col_index(cell: dict) -> int:
    return cell.get("col_index", 0)


def _is_header_cell(cell: dict) -> bool:
    """判断 cell 是否为表头 (Azure DI kind 字段)"""
    return cell.get("kind", "") == "columnHeader"


def _build_header_signature(cells: list[dict], header_rows: set[int]) -> str:
    """构建表头行的内容签名 (用于比较两个表格的表头是否相同)"""
    header_cells = [c for c in cells if _row_index(c) in header_rows]
    header_cells.sort(key=lambda c: (_row_index(c), _col_index(c)))
    texts = [_get_cell_text(c) for c in header_cells]
    return " | ".join(texts).lower()


def _detect_header_rows(cells: list[dict]) -> set[int]:
    """
    检测表格中的表头行。

    仅信任 Azure DI 的 cell.kind == "columnHeader" 显式标注。
    不做回退猜测 — 避免将续页无表头表格的第一行数据误判为表头。
    """
    header_rows = set()
    for cell in cells:
        if _is_header_cell(cell):
            header_rows.add(_row_index(cell))
    # 不设回退: 续页表格通常没有 kind 标注, 返回空 set 是正确的
    return header_rows


def _get_header_rows_or_none(cells: list[dict]) -> Optional[set[int]]:
    """
    获取表头行, 如果没有显式标注则返回 None。

    与 _detect_header_rows 的区别:
    - 有显式标注 → 返回表头行集合
    - 没有显式标注 → 返回 None (表示「不确定」, 而非「没有」)

    用于合并时区分「确定没有表头」和「不知道有没有表头」。
    """
    header_rows = set()
    for cell in cells:
        if _is_header_cell(cell):
            header_rows.add(_row_index(cell))
    return header_rows if header_rows else None


def _cells_to_html_table(cells: list[dict], row_count: int, col_count: int,
                          header_rows: Optional[set[int]] = None) -> str:
    """将 cells 渲染为 HTML 表格, 正确处理合并单元格"""
    # 构建网格
    grid = [[None for _ in range(col_count)] for _ in range(row_count)]
    for cell in cells:
        r, c = _row_index(cell), _col_index(cell)
        if 0 <= r < row_count and 0 <= c < col_count:
            grid[r][c] = cell

    if header_rows is None:
        header_rows = _detect_header_rows(cells)

    html_parts = ["<table>"]
    if header_rows:
        html_parts.append("<thead>")
        for row_idx in sorted(header_rows):
            html_parts.append(_render_row(grid, row_idx, col_count, "th", is_header=True))
        html_parts.append("</thead>")

    html_parts.append("<tbody>")
    body_rows = sorted(set(range(row_count)) - header_rows)
    for row_idx in body_rows:
        html_parts.append(_render_row(grid, row_idx, col_count, "td", is_header=False))
    html_parts.append("</tbody>")
    html_parts.append("</table>")

    return "\n".join(html_parts)


def _render_row(grid: list[list], row_idx: int, col_count: int, tag: str,
                is_header: bool = False) -> str:
    """渲染表格的一行"""
    parts = ["<tr>"]
    col = 0
    while col < col_count:
        cell = grid[row_idx][col] if row_idx < len(grid) else None
        if cell is None:
            col += 1
            continue

        # 只渲染合并区域的起始 cell
        if _row_index(cell) == row_idx and _col_index(cell) == col:
            attrs = []
            rs = cell.get("row_span") or 1
            cs = cell.get("col_span") or 1
            if rs > 1:
                attrs.append(f'rowspan="{rs}"')
            if cs > 1:
                attrs.append(f'colspan="{cs}"')
            # ★ 多层表头: scope 属性标记父子关系
            if is_header:
                scope = "colgroup" if cs > 1 else "col"
                attrs.append(f'scope="{scope}"')
            attr_str = " " + " ".join(attrs) if attrs else ""
            content = _get_cell_text(cell).replace("\n", "<br>")
            parts.append(f"<{tag}{attr_str}>{content}</{tag}>")
            col += cs
        else:
            col += 1
    parts.append("</tr>")
    return "\n".join(parts)


# ── 跨页合并主逻辑 ──────────────────────────────────────

def _calc_column_widths(cells: list[dict]) -> dict[int, float]:
    """计算每列的宽度 (基于 bbox x1 - x0)"""
    col_widths: dict[int, list[float]] = defaultdict(list)
    for cell in cells:
        bbox = cell.get("bbox")
        if bbox and len(bbox) == 4:
            width = bbox[2] - bbox[0]
            col_widths[_col_index(cell)].append(width)
    return {c: sum(ws) / len(ws) for c, ws in col_widths.items() if ws}


def _column_width_similarity(cols_a: dict[int, float], cols_b: dict[int, float]) -> float:
    """比较两组的列宽比例相似度"""
    common = set(cols_a.keys()) & set(cols_b.keys())
    if len(common) < 2:
        return 0.0

    def ratios(cols):
        vals = [cols[k] for k in sorted(common)]
        total = sum(vals)
        return [v / total for v in vals] if total > 0 else []

    ra, rb = ratios(cols_a), ratios(cols_b)
    if len(ra) != len(rb):
        return 0.0

    diffs = [abs(a - b) for a, b in zip(ra, rb)]
    return 1.0 - sum(diffs) / len(diffs)


def _tables_likely_same(table_a: dict, table_b: dict) -> bool:
    """
    判断两个表格是否可能是同一表格的延续。

    支持三种场景:
    1. 两表都有表头标注 → 比较表头签名
    2. 仅一表有表头标注 (续页无表头) → 仅比较列结构
    3. 两表都无表头标注 → 仅比较列结构
    """
    # 条件1: 列数相同
    if table_a.get("col_count", 0) != table_b.get("col_count", 0):
        return False

    # 条件2: 列宽比例相似
    cols_a = _calc_column_widths(table_a.get("cells", []))
    cols_b = _calc_column_widths(table_b.get("cells", []))
    if _column_width_similarity(cols_a, cols_b) < 0.7:
        return False

    # 条件3: 表头比较 (仅当双方都有显式表头标注时)
    h_rows_a = _detect_header_rows(table_a.get("cells", []))
    h_rows_b = _detect_header_rows(table_b.get("cells", []))

    if h_rows_a and h_rows_b:
        # 两表都有表头 → 比较签名
        sig_a = _build_header_signature(table_a.get("cells", []), h_rows_a)
        sig_b = _build_header_signature(table_b.get("cells", []), h_rows_b)
        if sig_a and sig_b:
            words_a = set(sig_a.split())
            words_b = set(sig_b.split())
            if words_a and words_b:
                jaccard = len(words_a & words_b) / len(words_a | words_b)
                if jaccard < HEADER_SIMILARITY_THRESHOLD:
                    return False
    # else: 一方或双方无显式表头 → 跳过签名比较, 仅靠列结构匹配
    # 典型场景: Page1 有表头, Page2-5 纯数据行无表头标注

    return True


def _is_continuation_table(table: dict, prev_table: dict) -> bool:
    """
    判断 table 是否是 prev_table 在同一文档中的延续。

    条件:
    1. table 在页面顶部 (y 坐标小)
    2. prev_table 在上一页底部
    3. 列数相同
    4. 表头相似
    """
    cells = table.get("cells", [])
    prev_cells = prev_table.get("cells", [])

    if not cells or not prev_cells:
        return False

    # table 的 page_numbers
    pages = set(table.get("page_numbers", []))
    prev_pages = set(prev_table.get("page_numbers", []))

    # 必须在不同页面
    if pages == prev_pages:
        return False

    # 页面连续性
    if pages and prev_pages:
        min_page = min(pages)
        max_prev = max(prev_pages)
        if min_page != max_prev + 1:
            # 不连续 → 不是同一表格
            return False

    return _tables_likely_same(table, prev_table)


def merge_cross_page_tables(all_tables: list[dict]) -> list[dict]:
    """
    跨页表格合并入口。

    流程:
    1. 按页码排序所有表格
    2. 检测跨页延续关系
    3. 从续页表格中移除重复的表头行
    4. 合并为单个逻辑表格

    Args:
        all_tables: 所有页面的表格列表 (来自 Azure DI 的标准化 table dict)

    Returns:
        合并后的表格列表 (跨页表格已合并, 单页表格保持原样)
    """
    if len(all_tables) <= 1:
        return all_tables

    # 按第一页排序
    tables = sorted(all_tables, key=lambda t: min(t.get("page_numbers", [999])))

    merged = []
    i = 0
    while i < len(tables):
        current = tables[i]

        # 检查后续表格是否为当前表格的延续
        continuation_group = [current]
        j = i + 1
        while j < len(tables):
            if _is_continuation_table(tables[j], continuation_group[-1]):
                continuation_group.append(tables[j])
                j += 1
            else:
                break

        if len(continuation_group) == 1:
            # 单页表格, 直接保留
            merged.append(current)
        else:
            # 跨页表格, 合并
            merged_table = _merge_table_group(continuation_group)
            merged.append(merged_table)
            logger.info(
                f"跨页表格合并: {len(continuation_group)} 个分片 → "
                f"1 个表格 ({merged_table['row_count']} 行 × {merged_table['col_count']} 列), "
                f"页码范围: {merged_table['page_numbers']}"
            )

        i = j

    return merged


def _merge_table_group(table_group: list[dict]) -> dict:
    """
    合并一组属于同一表格的跨页分片。

    规则:
    - 第一个表格 (首页): 保留所有行, 表头来自 Azure DI 显式标注
    - 后续表格 (续页):
      · 有显式表头标注 (kind="columnHeader") → 移除表头行, 保留数据行
      · 无显式表头标注 → 全部行视为数据, 直接追加 (← 仅首页有表头的场景)
    - Cell row_index 重新映射为全局行号
    """
    if len(table_group) == 1:
        return table_group[0]

    first = table_group[0]
    first_header_rows = _detect_header_rows(first.get("cells", []))
    col_count = first.get("col_count", 0)

    all_cells = []
    global_row_offset = 0
    all_page_numbers = set()
    total_headers_removed = 0

    for idx, table in enumerate(table_group):
        cells = table.get("cells", [])
        all_page_numbers.update(table.get("page_numbers", []))

        if idx == 0:
            # 首页: 保留所有行, 不修改 rowspan/colspan
            for cell in cells:
                new_cell = dict(cell)
                new_cell["row_index"] = _row_index(cell)
                # ★ 保留合并单元格属性
                new_cell["col_span"] = cell.get("col_span", 1)
                new_cell["row_span"] = cell.get("row_span", 1)
                all_cells.append(new_cell)

        else:
            # 续页: 仅移除 Azure DI 显式标注的表头行
            # ★ 安全规则: 如果表头 cell 有 rowspan > 1, 不移除 (延伸到数据行)
            continuation_headers = _get_header_rows_or_none(cells)

            if continuation_headers is None:
                logger.debug(
                    f"  续页 (idx={idx}): 无显式表头标注, 全部 {len(cells)} cells 保留"
                )
                continuation_headers = set()
            else:
                # 过滤掉 rowspan > 1 的表头行
                safe_headers = set()
                for row_idx in continuation_headers:
                    has_spanning_cell = any(
                        _row_index(c) == row_idx and (c.get("row_span") or 1) > 1
                        for c in cells
                    )
                    if has_spanning_cell:
                        logger.debug(
                            f"  保留跨行表头: row={row_idx} (rowspan > 1, 延伸到数据行)"
                        )
                    else:
                        safe_headers.add(row_idx)
                continuation_headers = safe_headers

            for cell in cells:
                row = _row_index(cell)

                if row in continuation_headers:
                    logger.debug(
                        f"  移除续页表头: idx={idx}, row={row}, "
                        f"content='{_get_cell_text(cell)[:40]}'"
                    )
                    total_headers_removed += 1
                    continue

                # 数据行: 重新映射 row_index
                new_cell = dict(cell)

                # ★ 保留合并单元格属性
                new_cell["col_span"] = cell.get("col_span", 1)
                new_cell["row_span"] = cell.get("row_span", 1)

                # 计算行偏移
                body_rows = sorted(set(
                    _row_index(c) for c in cells
                    if _row_index(c) not in continuation_headers
                ))
                if body_rows and row in body_rows:
                    body_offset = body_rows.index(row)
                    new_cell["row_index"] = (
                        global_row_offset +
                        first.get("row_count", 0) +
                        body_offset
                    )
                else:
                    new_cell["row_index"] = (
                        global_row_offset +
                        first.get("row_count", 0) +
                        row
                    )
                all_cells.append(new_cell)

            # 更新全局行偏移
            body_row_count = len(set(
                _row_index(c) for c in cells
                if _row_index(c) not in continuation_headers
            ))
            global_row_offset += body_row_count

    # 计算新的总行数
    all_rows = set(_row_index(c) for c in all_cells)
    new_row_count = max(all_rows) + 1 if all_rows else 0

    # 生成合并后的 HTML
    merged_html = _cells_to_html_table(
        all_cells,
        new_row_count,
        col_count,
        header_rows=first_header_rows,
    )

    return {
        "col_count": col_count,
        "row_count": new_row_count,
        "cells": all_cells,
        "page_numbers": sorted(all_page_numbers),
        "caption": first.get("caption"),
        "footnotes": first.get("footnotes", []),
        "table_html": merged_html,
        "_merged_from": len(table_group),
        "_header_rows_removed": total_headers_removed,
    }


# ── 表头补全: 跨页表格保持分页, 补全缺失表头 ──────────

def _extract_header_cells(table: dict) -> list[dict]:
    """
    提取表格中的表头 cells (基于 Azure DI kind 标注)。

    如果无标注, 取所有 cells 中 row_index == 0 的行作为表头。
    """
    cells = table.get("cells", [])
    # 优先使用 kind 标注
    header_cells = [c for c in cells if c.get("kind") == "columnHeader"]
    if header_cells:
        return header_cells

    # 回退: 第一行
    first_row = min((_row_index(c) for c in cells), default=-1)
    if first_row >= 0:
        header_cells = [c for c in cells if _row_index(c) == first_row]
    return header_cells


def _prepend_header_to_table(table: dict, header_cells: list[dict]) -> dict:
    """
    在表格数据行前面插入表头行。

    - 调整所有现有 cell 的 row_index (下移 header 行数)
    - 插入 header cells (row_index=0,1,...)
    - 重新生成 HTML
    """
    cells = table.get("cells", [])
    if not cells or not header_cells:
        return table

    # 计算 header 占用的行数
    header_rows = sorted(set(_row_index(c) for c in header_cells))
    header_row_count = len(header_rows)

    # 调整现有 cells 的 row_index
    new_cells = []
    for cell in cells:
        new_cell = dict(cell)
        new_cell["row_index"] = _row_index(cell) + header_row_count
        new_cells.append(new_cell)

    # 插入 header cells (保持原始 col_index 和 span)
    for hc in header_cells:
        new_cell = dict(hc)
        new_cell["row_index"] = _row_index(hc)  # 保持相对位置
        new_cells.append(new_cell)

    # 更新 row_count
    all_rows = set(_row_index(c) for c in new_cells)
    new_row_count = max(all_rows) + 1 if all_rows else 0
    col_count = table.get("col_count", 0)

    # 重新生成 HTML
    new_html = _cells_to_html_table(new_cells, new_row_count, col_count)

    return {
        **table,
        "cells": new_cells,
        "row_count": new_row_count,
        "table_html": new_html,
        "_header_completed": True,
        "_header_source": "copied from first page",
    }


def complete_table_headers(pages_data: list[dict]) -> list[dict]:
    """
    跨页表格表头补全 — 保持按页存储, 为缺失表头的续页补全表头。

    与 apply_cross_page_table_merge 的区别:
    - merge 模式: 所有分片合并为一个逻辑表格, 放入首页
    - complete 模式: 表格分页保留在各自页面, 仅为续页补全表头

    处理逻辑:
    1. 检测跨页表格延续组 (与 merge 相同)
    2. 从首页提取表头 cells
    3. 对续页: 如果没有显式表头 → 从首页复制表头插入

    Args:
        pages_data: 页面数据列表

    Returns:
        修改后的 pages_data
    """
    # 收集所有表格 (保留页码信息)
    all_tables_with_page = []
    for page in pages_data:
        page_num = page["page_number"]
        for table in page.get("tables", []):
            all_tables_with_page.append((page_num, table))

    if len(all_tables_with_page) <= 1:
        return pages_data

    tables_only = [t for _, t in all_tables_with_page]

    # 检测延续组
    continuation_groups = _detect_continuation_groups(tables_only)

    total_completed = 0
    total_found = 0

    for group in continuation_groups:
        if len(group) <= 1:
            continue

        total_found += 1

        # 首页表格 → 提取表头
        first_table = group[0]
        header_cells = _extract_header_cells(first_table)

        if not header_cells:
            logger.debug(f"  跨页表格 {total_found}: 首页无表头, 跳过")
            continue

        # 对每个续页表格: 检查是否缺表头, 缺则补全
        for cont_table in group[1:]:
            cont_headers = _detect_header_rows(cont_table.get("cells", []))

            if not cont_headers:
                # ★ 续页表头缺失 → 补全
                logger.info(
                    f"  表头补全: 续页表格 (pages={cont_table.get('page_numbers')}) "
                    f"无表头 → 从首页复制 {len(header_cells)} cells"
                )

                # 更新续页表格 (原地修改)
                updated = _prepend_header_to_table(cont_table, header_cells)

                # 将修改写回 pages_data
                for page_data in pages_data:
                    for i, t in enumerate(page_data.get("tables", [])):
                        if t is cont_table:  # 直接引用比较
                            page_data["tables"][i] = updated
                            break

                total_completed += 1
            else:
                logger.debug(
                    f"  续页表格 (pages={cont_table.get('page_numbers')}) "
                    f"已有 {len(cont_headers)} 表头行, 跳过"
                )

    if total_found > 0:
        logger.info(
            f"跨页表格处理: {total_found} 组, "
            f"{total_completed} 个续页表头补全"
        )

    return pages_data


def _detect_continuation_groups(tables: list[dict]) -> list[list[dict]]:
    """检测跨页表格延续组 (返回分组, 每组是一系列延续的表格)"""
    if len(tables) <= 1:
        return [tables] if tables else []

    # 按首页排序
    sorted_tables = sorted(tables, key=lambda t: min(t.get("page_numbers", [999])))

    groups = []
    current_group = [sorted_tables[0]]

    for i in range(1, len(sorted_tables)):
        if _is_continuation_table(sorted_tables[i], current_group[-1]):
            current_group.append(sorted_tables[i])
        else:
            groups.append(current_group)
            current_group = [sorted_tables[i]]

    groups.append(current_group)
    return groups


# ── 便捷入口: 对整个文档的表格进行跨页合并 ──────────────

def apply_cross_page_table_merge(pages_data: list[dict]) -> list[dict]:
    """
    对 RAG Pipeline 的 pages_data 进行跨页表格合并。

    直接修改每个页面的 tables 列表, 替换为合并后的表格。

    Args:
        pages_data: _group_azure_results_by_page 的输出

    Returns:
        修改后的 pages_data (就地修改)
    """
    # 收集所有表格
    all_tables = []
    for page in pages_data:
        all_tables.extend(page.get("tables", []))

    if not all_tables:
        return pages_data

    # 合并
    merged_tables = merge_cross_page_tables(all_tables)

    if len(merged_tables) == len(all_tables):
        logger.debug("跨页表格检测: 无跨页表格")
        return pages_data

    logger.info(
        f"跨页表格合并: {len(all_tables)} 个原始表格 → "
        f"{len(merged_tables)} 个逻辑表格 ",
    )

    # 重建页面表格分配: 将合并后的表格放入首页所在页面
    table_by_first_page: dict[int, list[dict]] = defaultdict(list)
    for table in merged_tables:
        first_page = min(table.get("page_numbers", [0]))
        table_by_first_page[first_page].append(table)

    for page in pages_data:
        page_num = page["page_number"]
        if page_num in table_by_first_page:
            page["tables"] = table_by_first_page[page_num]
        else:
            page["tables"] = []

    return pages_data

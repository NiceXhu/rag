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
# 表头相似度阈值: 续页表头与首页表头内容的 Jaccard 匹配阈值
HEADER_SIMILARITY_THRESHOLD = 0.7
# 数据行签名相似度阈值: 两表数据行列模式匹配的最低要求
DATA_SIMILARITY_THRESHOLD = 0.5


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

    基于 Azure DI 的 cell.kind == "columnHeader" 显式标注，
    但会验证标注行是否确实符合表头特征 (而非被误标的数据行)。
    """
    candidates = set()
    for cell in cells:
        if _is_header_cell(cell):
            candidates.add(_row_index(cell))
    if not candidates:
        return set()
    # 过滤被 OCR 误标为 columnHeader 的数据行
    return _validate_header_candidates(cells, candidates)


def _get_header_rows_or_none(cells: list[dict]) -> Optional[set[int]]:
    """
    获取表头行, 如果没有显式标注则返回 None。

    与 _detect_header_rows 的区别:
    - 有经过验证的显式标注 → 返回表头行集合
    - 没有显式标注 (或全被过滤) → 返回 None

    用于合并时区分「确定没有表头」和「不知道有没有表头」。
    """
    candidates = set()
    for cell in cells:
        if _is_header_cell(cell):
            candidates.add(_row_index(cell))
    if not candidates:
        return None
    validated = _validate_header_candidates(cells, candidates)
    return validated if validated else None


def _validate_header_candidates(
    cells: list[dict], candidate_rows: set[int],
) -> set[int]:
    """
    验证候选表头行是否确实是表头 (而非被 OCR 误标的数据行)。

    当任何候选行通过验证后，保留所有候选行 (维持原有的多层表头结构)。
    仅在所有候选行都被判定为「像数据」时才清空。
    """
    all_rows = sorted(set(_row_index(c) for c in cells))
    data_rows = [r for r in all_rows if r not in candidate_rows]

    if not data_rows:
        return candidate_rows  # 无法验证，保留全部

    validated = set()
    rejected = set()
    for row_idx in candidate_rows:
        if _row_looks_like_data(cells, row_idx, data_rows):
            rejected.add(row_idx)
            logger.debug(
                f"  表头验证: row={row_idx} 内容模式匹配数据行 → 从表头中移除"
            )
        else:
            validated.add(row_idx)

    if rejected and not validated:
        # 全部候选行都像数据 → 清空
        logger.debug(
            f"  表头验证: 全部 {len(rejected)} 个候选行被判定为数据 → 返回空表头"
        )
        return set()

    return validated  # 保留通过验证的行


def _row_looks_like_data(
    cells: list[dict], candidate_row_idx: int, data_row_indices: list[int],
) -> bool:
    """
    判断候选行内容是否更接近数据行 (而非表头)。

    多级级联检测:
    1. 候选行有 ≥ 1/4 空 cell → 强信号 (表头极少留空)
    2. 逐列比较候选行与数据行的内容长度模式
       — 仅当数据行 ≥ 3 行且 ≥ 80% 列匹配时视为强信号
    3. 候选行平均长度 ≥ 数据行平均长度 → 辅助信号

    返回策略: 信号1 单独成立即可; 信号2 需数据行 ≥ 3 + 配合信号3 同时成立。
              小样本 (< 3 数据行) 时统计不可靠，仅依赖信号1。
    """
    cand_cells = [c for c in cells if _row_index(c) == candidate_row_idx]
    cand_by_col = {_col_index(c): _get_cell_text(c) for c in cand_cells}

    all_cols = sorted(set(_col_index(c) for c in cand_cells))
    non_empty = [t for t in cand_by_col.values() if t]

    # 信号 1: 候选行有 ≥ 1/4 空 cell → 强信号 (表头极少留空列)
    if len(all_cols) >= 3 and len(non_empty) / len(all_cols) < 0.75:
        return True

    # 小样本保护: 数据行不足 3 行时统计不可靠，不做信号2判断
    if len(data_row_indices) < 3:
        return False

    # 收集数据行每列的内容
    data_len_by_col: dict[int, list[int]] = defaultdict(list)
    for c in cells:
        if _row_index(c) in data_row_indices:
            text = _get_cell_text(c)
            data_len_by_col[_col_index(c)].append(len(text))

    if not data_len_by_col:
        return False

    # 信号 2: 逐列长度模式比较 (高阈值 — ≥ 80% 列匹配)
    matched_cols = 0
    compared_cols = 0
    for col in all_cols:
        cand_text = cand_by_col.get(col, "")
        cand_len = len(cand_text)
        col_lens = data_len_by_col.get(col, [])
        if not col_lens:
            compared_cols += 1
            continue
        col_avg = sum(col_lens) / len(col_lens)
        # 候选行该列与数据同列平均长度差异 < 40% → 匹配
        if col_avg > 0 and abs(cand_len - col_avg) / max(cand_len, col_avg) < 0.4:
            matched_cols += 1
        compared_cols += 1

    # 信号 3: 候选行平均长度 ≥ 数据行平均长度
    cand_avg_len = sum(len(t) for t in non_empty) / max(len(non_empty), 1)
    all_data_lens = [
        len(_get_cell_text(c))
        for c in cells if _row_index(c) in data_row_indices
    ]
    data_avg_len = sum(all_data_lens) / max(len(all_data_lens), 1)
    longer_than_data = data_avg_len > 0 and cand_avg_len >= data_avg_len

    # 信号 2 + 信号 3 同时成立 → 像数据
    if compared_cols >= 3 and matched_cols / compared_cols >= 0.8 and longer_than_data:
        return True

    return False


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


def _build_data_signature(table: dict, exclude_rows: set[int]) -> dict[int, dict]:
    """
    提取表格每列的数据行内容模式签名（排除指定行如 header）。

    对每列统计:
      - avg_len:  内容平均长度
      - is_numeric: 该列是否为数值型 (>50% cells 可解析为数字)
      - empty_ratio: 空 cell 比例

    返回值: {col_index: {avg_len, is_numeric, empty_ratio}, ...}
    用于比较两表的数据行是否属于同一表格。
    """
    cells = table.get("cells", [])
    cols_data: dict[int, list[str]] = defaultdict(list)

    for cell in cells:
        if _row_index(cell) in exclude_rows:
            continue
        text = _get_cell_text(cell)
        cols_data[_col_index(cell)].append(text)

    if not cols_data:
        return {}

    signature = {}
    for col_idx, texts in cols_data.items():
        lengths = [len(t) for t in texts]
        numeric_count = sum(
            1 for t in texts
            if t and re.match(r'^[\d,.%+\-×\s]+$', t)
        )
        total = len(texts)
        signature[col_idx] = {
            "avg_len": sum(lengths) / total if total else 0,
            "is_numeric": (numeric_count / total) >= 0.5 if total else False,
            "empty_ratio": sum(1 for t in texts if not t) / total if total else 0,
        }
    return signature


def _data_signature_similarity(
    sig_a: dict[int, dict], sig_b: dict[int, dict],
) -> float:
    """
    比较两个数据行签名的列模式相似度。

    返回 0.0 ~ 1.0 的相似度分数。
    """
    common = set(sig_a.keys()) & set(sig_b.keys())
    if len(common) < 2:
        return 0.0

    scores = []
    for col_idx in common:
        ca, cb = sig_a[col_idx], sig_b[col_idx]

        # 数值型/文本型必须一致
        if ca["is_numeric"] != cb["is_numeric"]:
            scores.append(0.0)
            continue

        # 长度差异分数
        max_len = max(ca["avg_len"], cb["avg_len"])
        if max_len > 0:
            len_score = 1.0 - abs(ca["avg_len"] - cb["avg_len"]) / max_len
        else:
            len_score = 1.0  # 两列都为空

        # 空值比例差异分数
        empty_diff = abs(ca["empty_ratio"] - cb["empty_ratio"])
        empty_score = 1.0 - empty_diff

        scores.append((len_score + empty_score) / 2.0)

    return sum(scores) / len(scores) if scores else 0.0


def _first_row_is_distinct_header(table: dict, prev_table: dict) -> bool:
    """
    判断 table 的第一行是否是一个独立的新表头（而非 prev_table 的数据延续）。

    检测信号:
    1. table 第一行有 col_span > 1 的合并单元格 → 强信号
    2. table 第一行有 kind="columnHeader" 标注 → 强信号
    3. table 第一行内容短 (< 25 chars average) 且 ≥2/3 列非空 → 表头形态
    4. table 第一行与 prev_table 最后几行的列类型/长度模式比较 → 突变检测
    5. 跨表数据列类型一致性: table 第一行 vs prev_table 数据行的类型分布

    返回 True 表示 table 的第一行很可能是新表格的表头，不应合并。
    """
    cells = table.get("cells", [])
    prev_cells = prev_table.get("cells", [])

    if not cells:
        return False

    # 确定 table 的第一行
    first_row_idx = min(_row_index(c) for c in cells)
    first_row_cells = [c for c in cells if _row_index(c) == first_row_idx]
    first_row_cells.sort(key=_col_index)

    # 信号 1: 第一行有合并单元格 (col_span > 1)
    for cell in first_row_cells:
        if (cell.get("col_span") or 1) > 1:
            return True

    # 信号 2: 第一行有显式 columnHeader 标注
    # (已通过 _validate_header_candidates 的 col_span/columnHeader 检查，
    #  此处再次确认 — 与 prev_table 有相同标注且通过验证则不是新表头)
    has_kind_header = any(c.get("kind") == "columnHeader" for c in first_row_cells)

    # 信号 3: 第一行内容短且密集 (典型表头形态)
    texts = [_get_cell_text(c) for c in first_row_cells]
    non_empty = [t for t in texts if t]
    avg_len = sum(len(t) for t in non_empty) / len(non_empty) if non_empty else 0
    fill_ratio = len(non_empty) / len(texts) if texts else 0

    looks_like_header = (
        avg_len < 25
        and fill_ratio >= 2.0 / 3.0
        and len(non_empty) >= 2
    )

    if not looks_like_header:
        return False

    # 如果没有 prev_table → 仅凭形态判断
    if not prev_cells:
        return looks_like_header

    prev_header_rows = _detect_header_rows(prev_cells)
    all_prev_rows = sorted(set(_row_index(c) for c in prev_cells))
    data_rows = [r for r in all_prev_rows if r not in prev_header_rows]

    if len(data_rows) < 2:
        return looks_like_header

    # ── 信号 4: prev 最后几行 vs table 第一行的列类型比较 ──
    # 取 prev_table 最后 3 行数据 (或全部数据行)
    sample_rows = data_rows[-3:] if len(data_rows) >= 3 else data_rows
    sample_cells = [c for c in prev_cells if _row_index(c) in sample_rows]

    # 构建 prev 数据每列的特征: 类型 (numeric/text) + 长度范围
    prev_col_profile: dict[int, dict] = {}
    for col in set(_col_index(c) for c in sample_cells):
        col_texts = [
            _get_cell_text(c)
            for c in sample_cells if _col_index(c) == col
        ]
        if not col_texts:
            continue
        lengths = [len(t) for t in col_texts]
        numeric_count = sum(
            1 for t in col_texts
            if t and re.match(r'^[\d,.%+\-×\s]+$', t)
        )
        prev_col_profile[col] = {
            "min_len": min(lengths),
            "max_len": max(lengths),
            "is_numeric": numeric_count / len(col_texts) >= 0.5,
        }

    # 比较 table 第一行每列 vs prev 数据列模式
    type_matched = 0
    type_mismatched = 0
    len_in_range = 0
    total_compared = 0

    for cell in first_row_cells:
        col = _col_index(cell)
        profile = prev_col_profile.get(col)
        if profile is None:
            continue
        total_compared += 1
        text = _get_cell_text(cell)
        cur_len = len(text)

        # 类型比较: numeric vs text
        cur_is_numeric = bool(text and re.match(r'^[\d,.%+\-×\s]+$', text))
        if cur_is_numeric == profile["is_numeric"]:
            type_matched += 1
        else:
            type_mismatched += 1

        # 长度范围比较
        if profile["min_len"] <= cur_len <= profile["max_len"] * 1.5:
            len_in_range += 1

    if total_compared < 2:
        # 可比较的列太少，如果有 kind 标注则信任它是同一表格的延续
        if has_kind_header:
            return False  # prev 也有同标注，信任为同一表格
        return looks_like_header

    # 类型匹配率 > 80% → 数据延续
    type_match_ratio = type_matched / total_compared
    if type_match_ratio > 0.8:
        return False  # 列类型高度一致 → 同一表格的数据延续

    # 类型不匹配 ≥1 且长度不在范围内 ≥ 一半列 → 新表头
    if type_mismatched >= 1 and len_in_range / total_compared < 0.5:
        return True

    return False


def _tables_likely_same(
    table_a: dict, table_b: dict,
    first_table: Optional[dict] = None,
) -> bool:
    """
    判断两个表格是否可能是同一表格的延续。

    判断链:
    1. 列数相同 (硬条件)
    2. 列宽比例相似 (硬条件, ≥ 0.7)
    3. 双方都有显式表头 → 比较表头签名 + 数据连续性交叉检查
    4. 一方或双方无显式表头 → 数据连续性检查:
       a) table_b 第一行是否为独立新表头 → False
       b) 两表数据行签名比较 → 不相似 → False
    5. 全局基线检查: 将 table_b 与集团首页 (first_table) 做数据模式比对
    """
    cells_a = table_a.get("cells", [])
    cells_b = table_b.get("cells", [])

    # 条件1: 列数相同
    if table_a.get("col_count", 0) != table_b.get("col_count", 0):
        return False

    # 条件2: 列宽比例相似
    cols_a = _calc_column_widths(cells_a)
    cols_b = _calc_column_widths(cells_b)
    if _column_width_similarity(cols_a, cols_b) < 0.7:
        return False

    # 条件3: 表头比较
    h_rows_a = _detect_header_rows(cells_a)
    h_rows_b = _detect_header_rows(cells_b)

    if h_rows_a and h_rows_b:
        # 两表都有表头 → 比较签名
        sig_a = _build_header_signature(cells_a, h_rows_a)
        sig_b = _build_header_signature(cells_b, h_rows_b)
        if sig_a and sig_b:
            words_a = set(sig_a.split())
            words_b = set(sig_b.split())
            if words_a and words_b:
                jaccard = len(words_a & words_b) / len(words_a | words_b)
                if jaccard < HEADER_SIMILARITY_THRESHOLD:
                    return False
                # Jaccard 通过 → 继续数据连续性检查 (不直接 return)
                # 防止: 两表表头文本相似但 table_b 实际是另一个表的开头

        # 数据连续性交叉检查: table_b 第一行是否为数据延续
        # 如果 table_b 第一行看起来像新表头 (不同于 table_a 数据模式) → 不同表格
        if _first_row_is_distinct_header(table_b, table_a):
            logger.debug(
                f"  表格合并跳过: 双方有表头但 table_b 第一行为独立新表头 "
                f"(pages={table_a.get('page_numbers')} vs {table_b.get('page_numbers')})"
            )
            return False

        # 全局基线检查 (如果有)
        if first_table is not None and first_table is not table_a:
            sig_first = _build_header_signature(
                first_table.get("cells", []),
                _detect_header_rows(first_table.get("cells", [])),
            )
            if sig_first and sig_b:
                words_first = set(sig_first.split())
                if words_first and words_b:
                    jaccard_first = len(words_first & words_b) / len(words_first | words_b)
                    if jaccard_first < HEADER_SIMILARITY_THRESHOLD:
                        logger.debug(
                            f"  表格合并跳过: table_b 表头与集团首页表头不匹配 "
                            f"(jaccard={jaccard_first:.2f} < {HEADER_SIMILARITY_THRESHOLD})"
                        )
                        return False

        return True

    # 条件4: 一方或双方无显式表头 → 数据连续性检查

    # 4a: 边界检测 — table_b 有表头但 table_a 没有
    # 这很可能是新表格的开端, 需要与集团首页表头做交叉比对
    if h_rows_b and not h_rows_a:
        if first_table is not None:
            h_first = _detect_header_rows(first_table.get("cells", []))
            if h_first:
                # table_b 的表头 vs 集团首页的表头
                sig_b_hdr = _build_header_signature(cells_b, h_rows_b)
                sig_first_hdr = _build_header_signature(
                    first_table.get("cells", []), h_first,
                )
                if sig_b_hdr and sig_first_hdr:
                    words_b = set(sig_b_hdr.split())
                    words_first = set(sig_first_hdr.split())
                    if words_b and words_first:
                        jaccard = len(words_b & words_first) / len(words_b | words_first)
                        if jaccard < HEADER_SIMILARITY_THRESHOLD:
                            logger.debug(
                                f"  表格合并跳过: table_b 表头与首页表头不匹配 (边界检测) "
                                f"(jaccard={jaccard:.2f} < {HEADER_SIMILARITY_THRESHOLD})"
                            )
                            return False
            else:
                # 集团首页也无表头, table_b 突然出现表头 → 可能是新表
                if _first_row_is_distinct_header(table_b, table_a):
                    return False
                # 否则继续, 让数据签名来判断

    # 4b: table_b 的第一行是否是新表头
    if _first_row_is_distinct_header(table_b, table_a):
        logger.debug(
            f"  表格合并跳过: table_b 第一行为独立新表头 "
            f"(pages={table_a.get('page_numbers')} vs {table_b.get('page_numbers')})"
        )
        return False

    # 4c: 比较两表数据行内容模式
    sig_a = _build_data_signature(table_a, h_rows_a)
    sig_b = _build_data_signature(table_b, h_rows_b)

    if sig_a and sig_b:
        similarity = _data_signature_similarity(sig_a, sig_b)
        if similarity < DATA_SIMILARITY_THRESHOLD:
            logger.debug(
                f"  表格合并跳过: 数据内容模式不相似 "
                f"(similarity={similarity:.2f} < {DATA_SIMILARITY_THRESHOLD})"
            )
            return False

    # 4d: 全局基线检查 — table_b vs 集团首页
    # 对任意一方无显式表头的情况都做 (含 P10无表头→P11有新表头 已通过边界检测的场景)
    if first_table is not None and first_table is not table_a:
        h_first = _detect_header_rows(first_table.get("cells", []))
        sig_first = _build_data_signature(first_table, h_first)
        if sig_first and sig_b:
            similarity_to_first = _data_signature_similarity(sig_first, sig_b)
            if similarity_to_first < DATA_SIMILARITY_THRESHOLD:
                logger.debug(
                    f"  表格合并跳过: table_b 数据模式与集团首页不匹配 "
                    f"(similarity={similarity_to_first:.2f} < {DATA_SIMILARITY_THRESHOLD})"
                )
                return False

    return True


def _is_continuation_table(
    table: dict, prev_table: dict, first_table: Optional[dict] = None,
) -> bool:
    """
    判断 table 是否是 prev_table 在同一文档中的延续。

    条件:
    1. 在不同页面
    2. 页面连续 (prev 最大页 + 1 == table 最小页)
    3. 列数 + 列宽 + 表头/数据模式符合 (见 _tables_likely_same)

    first_table 为集团首页表格，用于全局基线比对 (可选)。
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

    return _tables_likely_same(table, prev_table, first_table=first_table)


def merge_cross_page_tables(all_tables: list[dict]) -> list[dict]:
    """
    跨页表格合并入口。

    流程:
    1. 使用 _detect_continuation_groups 检测跨页延续关系 (含全局基线)
    2. 对每组延续表格调用 _merge_table_group 合并
    3. 单页表格直接保留
    """
    if len(all_tables) <= 1:
        return all_tables

    # 按第一页排序
    tables = sorted(all_tables, key=lambda t: min(t.get("page_numbers", [999])))

    # 使用统一的分组检测逻辑 (含全局基线比较)
    groups = _detect_continuation_groups(tables)

    merged = []
    for group in groups:
        if len(group) == 1:
            merged.append(group[0])
        else:
            merged_table = _merge_table_group(group)
            merged.append(merged_table)
            logger.info(
                f"跨页表格合并: {len(group)} 个分片 → "
                f"1 个表格 ({merged_table['row_count']} 行 × {merged_table['col_count']} 列), "
                f"页码范围: {merged_table['page_numbers']}"
            )

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

    # ★ 安全裁剪: 跨页 rowspan 不超出合并后总行数
    # OCR 按页独立标注 rowspan, 合并后需确保 rowspan ≤ 剩余行数
    rowspan_clipped = 0
    for cell in all_cells:
        rs = cell.get("row_span", 1)
        r = _row_index(cell)
        max_possible = new_row_count - r
        if rs > max_possible:
            cell["row_span"] = max_possible
            rowspan_clipped += 1
    if rowspan_clipped > 0:
        logger.debug(
            f"  合并表格: {rowspan_clipped} 个 cell 的 rowspan 被裁剪至边界"
        )

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
    提取表格中的表头 cells (通过 _detect_header_rows 验证)。

    仅返回通过 _validate_header_candidates 过滤后的表头行 cells。
    无显式标注或全被过滤 → 返回空列表。
    """
    cells = table.get("cells", [])
    validated_rows = _detect_header_rows(cells)  # 内部已调用 _validate_header_candidates
    if not validated_rows:
        return []
    return [c for c in cells if _row_index(c) in validated_rows]


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

    支持父子嵌套表格: 按页内 Y 位置将表格分链独立处理。

    Args:
        pages_data: 页面数据列表

    Returns:
        修改后的 pages_data
    """
    # 按 Y 位置构建位置锚定链
    chains = _build_position_anchored_chains(pages_data)

    if not chains:
        return pages_data

    total_completed = 0
    total_found = 0

    for chain in chains:
        if len(chain) <= 1:
            continue

        total_found += 1

        # 首页表格 → 提取表头
        first_table = chain[0]
        header_cells = _extract_header_cells(first_table)

        if not header_cells:
            logger.debug(f"  跨页表格 {total_found}: 首页无表头, 跳过")
            continue

        # 对每个续页表格: 检查是否缺表头, 缺则补全
        for cont_table in chain[1:]:
            cont_headers = _detect_header_rows(cont_table.get("cells", []))

            if not cont_headers:
                logger.info(
                    f"  表头补全: 续页表格 (pages={cont_table.get('page_numbers')}) "
                    f"无表头 → 从首页复制 {len(header_cells)} cells"
                )

                updated = _prepend_header_to_table(cont_table, header_cells)

                # 写回 pages_data
                for page_data in pages_data:
                    for i, t in enumerate(page_data.get("tables", [])):
                        if t is cont_table:
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
            f"跨页表格处理: {len(chains)} 条位置链, "
            f"{total_found} 组跨页, "
            f"{total_completed} 个续页表头补全"
        )

    return pages_data


def _detect_continuation_groups(tables: list[dict]) -> list[list[dict]]:
    """
    检测跨页表格延续组。

    每个候选表格需同时通过两项检查:
    1. 与前一个表格的 _is_continuation_table (相邻比较)
    2. 与集团首页表格的全局基线比较 (防止数据漂移)
    """
    if len(tables) <= 1:
        return [tables] if tables else []

    # 按首页排序
    sorted_tables = sorted(tables, key=lambda t: min(t.get("page_numbers", [999])))

    groups = []
    current_group = [sorted_tables[0]]

    for i in range(1, len(sorted_tables)):
        candidate = sorted_tables[i]
        prev_table = current_group[-1]
        first_table = current_group[0]

        # 相邻比较 (传递集团首页做边界检测)
        if not _is_continuation_table(candidate, prev_table,
                                       first_table=first_table):
            groups.append(current_group)
            current_group = [candidate]
            continue

        # 全局基线比较 (集团长度 > 1 时才启用)
        if len(current_group) >= 2:
            if not _is_continuation_of_group(candidate, current_group):
                groups.append(current_group)
                current_group = [candidate]
                continue

        current_group.append(candidate)

    groups.append(current_group)
    return groups


def _is_continuation_of_group(
    table: dict, group: list[dict],
) -> bool:
    """
    检查 table 是否为 group 的延续 (全局基线比较)。

    将 table 与 group 首页做比对，防止局部相似但全局漂移。
    长表格使用自适应阈值，容忍自然数据波动。

    条件:
    1. 与 group 首页的列宽比例 ≥ 0.7
    2. 与 group 首页的数据行签名相似度 ≥ 自适应阈值
    """
    if not group:
        return True

    first = group[0]
    cells = table.get("cells", [])
    first_cells = first.get("cells", [])

    # 列数检查
    if table.get("col_count", 0) != first.get("col_count", 0):
        return False

    # 列宽比例比较 (硬条件, 不降阈)
    cols = _calc_column_widths(cells)
    cols_first = _calc_column_widths(first_cells)
    if _column_width_similarity(cols, cols_first) < 0.7:
        logger.debug(
            f"  全局基线跳过: 列宽比例 vs 首页不匹配 "
            f"(pages={table.get('page_numbers')} vs group首页 {first.get('page_numbers')})"
        )
        return False

    # 数据行签名比较 — 长表格自适应阈值
    h_rows = _detect_header_rows(cells)
    h_first = _detect_header_rows(first_cells)

    sig = _build_data_signature(table, h_rows)
    sig_first = _build_data_signature(first, h_first)

    if sig and sig_first:
        similarity = _data_signature_similarity(sig, sig_first)

        # 自适应阈值: 长表格容忍更大漂移
        #   ≤5 页 → 0.50 (严格, 确保同一表格)
        #   5-15 页 → 线性降到 0.30
        #   >15 页 → 0.30 (仅防止类型级突变)
        group_len = len(group)
        if group_len <= 5:
            threshold = 0.50
        elif group_len <= 15:
            threshold = 0.50 - (group_len - 5) * 0.02  # 5→0.50, 15→0.30
        else:
            threshold = 0.30

        if similarity < threshold:
            logger.debug(
                f"  全局基线跳过: 数据模式与首页不匹配 "
                f"(similarity={similarity:.2f} < threshold={threshold:.2f}, "
                f"group_len={group_len}, "
                f"candidate pages={table.get('page_numbers')})"
            )
            return False

    return True


# ── 页内位置锚定: 父子嵌套表格 ────────────────────────────

def _get_table_y_position(table: dict) -> float:
    """获取表格在页面上的垂直起始位置 (取所有 cell bbox 的最小 Y)"""
    cells = table.get("cells", [])
    if not cells:
        return 0.0
    min_y = float('inf')
    for cell in cells:
        bbox = cell.get("bbox")
        if bbox and len(bbox) == 4 and bbox[1] > 0:
            min_y = min(min_y, float(bbox[1]))
    return min_y if min_y != float('inf') else 0.0


def _build_position_anchored_chains(
    pages_data: list[dict],
) -> list[list[dict]]:
    """
    按页内 Y 位置将表格分组为独立跨页链，支持父子嵌套表格。

    算法: 对每页按 Y 排序后的表格，与前一页做 Y 邻近 + 内容匹配。
    - 同一 Y 位置 + 通过 _tables_likely_same → 同一链 (跨页延续)
    - 无匹配 → 新链 (新表格开始)
    - 前一页有链但当前页无匹配 → 该链结束

    返回:
        chains: 每条链是一个列表 [P1-table, P2-table, ...]
                链内表格来自连续页面、相同 Y 位置锚定

    示例:
        P1: [父表 y=100, 子表 y=500]
        P2: [父表续 y=100, 子表续 y=500]
        → chain-0: [P1-父, P2-父续], chain-1: [P1-子, P2-子续]
    """
    # 收集所有表格带页面号
    tables_by_page: dict[int, list[dict]] = defaultdict(list)
    for page in pages_data:
        pn = page["page_number"]
        page_tables = sorted(page.get("tables", []), key=_get_table_y_position)
        if page_tables:
            tables_by_page[pn] = page_tables

    if not tables_by_page:
        return []

    sorted_pages = sorted(tables_by_page.keys())
    chains: list[list[dict]] = []      # 所有链
    prev_assignments: list[tuple[int, dict]] = []  # [(chain_idx, prev_table), ...]

    for page_num in sorted_pages:
        curr_tables = tables_by_page[page_num]

        if not prev_assignments:
            # 第一页: 每个表格各起一条链
            for table in curr_tables:
                chains.append([table])
                prev_assignments.append((len(chains) - 1, table))
            continue

        # 为每个当前表格找最佳的前页匹配
        used_prev: set[int] = set()
        curr_assignments: list[tuple[int, dict]] = []

        for curr_table in curr_tables:
            best_pi: Optional[int] = None
            best_score = 0.0

            for pi, (chain_idx, prev_table) in enumerate(prev_assignments):
                if pi in used_prev:
                    continue
                # 内容匹配第一
                first_of_chain = chains[chain_idx][0]
                if not _tables_likely_same(
                    curr_table, prev_table, first_table=first_of_chain,
                ):
                    continue

                # Y 邻近度打分 (越近越高)
                y_diff = abs(
                    _get_table_y_position(curr_table)
                    - _get_table_y_position(prev_table)
                )
                score = 1.0 / (1.0 + y_diff)

                if score > best_score:
                    best_score = score
                    best_pi = pi

            if best_pi is not None:
                chain_idx = prev_assignments[best_pi][0]
                chains[chain_idx].append(curr_table)
                used_prev.add(best_pi)
                curr_assignments.append((chain_idx, curr_table))
            else:
                # 无匹配 → 新链
                chains.append([curr_table])
                curr_assignments.append((len(chains) - 1, curr_table))

        prev_assignments = curr_assignments

    return chains


# ── 便捷入口: 对整个文档的表格进行跨页合并 ──────────────

def apply_cross_page_table_merge(pages_data: list[dict]) -> list[dict]:
    """
    对 RAG Pipeline 的 pages_data 进行跨页表格合并。

    支持父子嵌套表格: 按页内 Y 位置将表格分链，
    每条链独立做跨页合并，父子表格互不干扰。

    Args:
        pages_data: _group_azure_results_by_page 的输出

    Returns:
        修改后的 pages_data (就地修改)
    """
    # 按 Y 位置构建位置锚定链 (父子表格分离)
    chains = _build_position_anchored_chains(pages_data)

    if not chains:
        return pages_data

    total_original = sum(len(chain) for chain in chains)

    # 每条链独立合并
    all_merged = []
    for chain in chains:
        if len(chain) <= 1:
            all_merged.extend(chain)
        else:
            merged_parts = merge_cross_page_tables(chain)
            all_merged.extend(merged_parts)

    if len(all_merged) == total_original:
        logger.debug("跨页表格检测: 无跨页表格")
        return pages_data

    logger.info(
        f"跨页表格合并: {total_original} 个原始表格 "
        f"({len(chains)} 条位置链) → "
        f"{len(all_merged)} 个逻辑表格"
    )

    # 重建页面表格分配
    table_by_first_page: dict[int, list[dict]] = defaultdict(list)
    for table in all_merged:
        first_page = min(table.get("page_numbers", [0]))
        table_by_first_page[first_page].append(table)

    for page in pages_data:
        page_num = page["page_number"]
        page["tables"] = table_by_first_page.get(page_num, [])

    return pages_data

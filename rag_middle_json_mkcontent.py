# Copyright (c) Opendatalab. All rights reserved.
"""
RAG 后端 middle_json 到 Markdown 输出转换。

从 middle_json (pdf_info) 生成最终的 Markdown 和 content_list 输出。
遵循与 pipeline/vlm 后端相同的 union_make() 接口。

输出模式:
  MM_MD          — 标准 Markdown 文档
  NLP_MD         — NLP 友好的纯文本
  CONTENT_LIST   — 按阅读顺序的内容列表 JSON
  CONTENT_LIST_V2 — 增强版内容列表 JSON
"""
import re
from html import escape, unescape

from loguru import logger

from mineru.utils.config_reader import get_latex_delimiter_config
from mineru.utils.enum_class import MakeMode, BlockType, ContentType, ContentTypeV2
from mineru.backend.utils.markdown_utils import (
    escape_conservative_markdown_text,
    escape_text_block_markdown_prefix,
)

# ── LaTeX 定界符配置 ──────────────────────────────────────
_latex_config = get_latex_delimiter_config()
if _latex_config:
    _display_left = _latex_config.get('display', {}).get('left', '$$')
    _display_right = _latex_config.get('display', {}).get('right', '$$')
    _inline_left = _latex_config.get('inline', {}).get('left', '$')
    _inline_right = _latex_config.get('inline', {}).get('right', '$')
else:
    _display_left, _display_right = '$$', '$$'
    _inline_left, _inline_right = '$', '$'


def union_make(pdf_info: list[dict], mode: str, img_bucket_path: str = "") -> str | list:
    """
    统一的输出生成接口 (与 pipeline/vlm/office 后端一致)

    Args:
        pdf_info: middle_json["pdf_info"] 列表
        mode: 输出模式 (MakeMode.MM_MD, MakeMode.CONTENT_LIST 等)
        img_bucket_path: 图片路径前缀

    Returns:
        根据 mode 不同返回 str (Markdown) 或 list (content_list)
    """
    if mode in (MakeMode.MM_MD, MakeMode.NLP_MD):
        return _make_markdown(pdf_info, mode, img_bucket_path)
    elif mode == MakeMode.CONTENT_LIST:
        return _make_content_list(pdf_info, img_bucket_path)
    elif mode == MakeMode.CONTENT_LIST_V2:
        return _make_content_list_v2(pdf_info, img_bucket_path)
    else:
        logger.warning(f"Unknown MakeMode: {mode}, falling back to MM_MD")
        return _make_markdown(pdf_info, MakeMode.MM_MD, img_bucket_path)


def _make_markdown(pdf_info: list[dict], mode: str, img_bucket_path: str = "") -> str:
    """生成 Markdown 文档"""
    md_lines = []

    for page_info in pdf_info:
        blocks = page_info.get("preproc_blocks", [])
        for block in blocks:
            block_md = _block_to_markdown(block, img_bucket_path)
            if block_md:
                md_lines.append(block_md)

    return "\n\n".join(md_lines)


def _block_to_markdown(block: dict, img_bucket_path: str = "") -> str:
    """将单个 block 转为 Markdown"""

    if not isinstance(block, dict):
        return ""

    block_type = block.get("type", "")

    # 文本类 block
    if block_type == BlockType.TEXT:
        return _lines_to_text(block)
    elif block_type == BlockType.TITLE:
        level = block.get("level", 1)
        title_text = _lines_to_text(block)
        return f"{'#' * min(level, 6)} {title_text}"
    elif block_type == BlockType.ABSTRACT:
        return f"> **Abstract:** {_lines_to_text(block)}"

    # 数学公式
    elif block_type == BlockType.INTERLINE_EQUATION:
        formula = _extract_formula_from_block(block)
        if formula:
            return f"{_display_left}\n{formula}\n{_display_right}"
        return ""

    # 图片/图表/印章
    elif block_type in [BlockType.IMAGE, BlockType.CHART, BlockType.SEAL]:
        return _visual_block_to_markdown(block, img_bucket_path)

    # 表格
    elif block_type == BlockType.TABLE:
        return _table_block_to_markdown(block, img_bucket_path)

    # 列表
    elif block_type == BlockType.LIST:
        return _list_block_to_markdown(block, indent_level=0)

    # 代码
    elif block_type == BlockType.CODE:
        code_text = _lines_to_text(block)
        return f"```\n{code_text}\n```"

    # 引用文本
    elif block_type == BlockType.REF_TEXT:
        return f"> {_lines_to_text(block)}"

    return ""


def _lines_to_text(block: dict) -> str:
    """将 block 的 lines/spans 展开为纯文本, 含超链接回插"""
    parts = []
    for line in block.get("lines", []):
        for span in line.get("spans", []):
            # ★ 跳过被合并到其他 span 的跨行超链接片段
            if span.get("_hyperlink_merged"):
                continue

            span_type = span.get("type", "")
            content = span.get("content", "")

            # ★ PDF 超链接回插: 优先级最高的处理
            hyperlink = span.get("_hyperlink", "")
            if hyperlink and span_type == ContentType.TEXT:
                safe_text = escape_conservative_markdown_text(content)
                if safe_text.strip():
                    parts.append(f"[{safe_text}]({hyperlink})")
                else:
                    parts.append(safe_text)
            elif span_type == ContentType.TEXT:
                safe_text = escape_conservative_markdown_text(content)
                parts.append(safe_text)
            elif span_type == ContentType.INLINE_EQUATION:
                parts.append(f"{_inline_left}{content}{_inline_right}")
            elif span_type == ContentType.INTERLINE_EQUATION:
                parts.append(f"{_display_left}{content}{_display_right}")
            elif span_type == ContentType.HYPERLINK:
                url = span.get("url", "")
                parts.append(f"[{content}]({url})")
            else:
                parts.append(content)

    return "".join(parts)


def _extract_formula_from_block(block: dict) -> str:
    """从公式 block 中提取 LaTeX 公式"""
    for line in block.get("lines", []):
        for span in line.get("spans", []):
            if span.get("type") in [ContentType.INTERLINE_EQUATION, ContentType.EQUATION]:
                return span.get("content", "").strip()
    return ""


def _visual_block_to_markdown(block: dict, img_bucket_path: str = "") -> str:
    """将图片/图表/印章 block 转为 Markdown 图片语法"""
    parts = []

    for sub_block in block.get("blocks", []):
        sub_type = sub_block.get("type", "")
        for line in sub_block.get("lines", []):
            for span in line.get("spans", []):
                if span.get("type") == ContentType.IMAGE:
                    image_path = span.get("image_path", "")
                    if img_bucket_path and image_path:
                        image_path = f"{img_bucket_path}/{image_path}"
                    alt_text = _extract_caption_from_block(block)
                    parts.append(f"![{alt_text}]({image_path})")
                elif span.get("type") in [ContentType.TEXT, ContentType.INLINE_EQUATION]:
                    parts.append(span.get("content", ""))

    return "\n".join(parts)


def _table_block_to_markdown(block: dict, img_bucket_path: str = "") -> str:
    """将表格 block 转为 Markdown"""
    parts = []

    for sub_block in block.get("blocks", []):
        sub_type = sub_block.get("type", "")
        if sub_type == BlockType.TABLE_BODY:
            for line in sub_block.get("lines", []):
                for span in line.get("spans", []):
                    if span.get("type") == ContentType.TABLE:
                        html = span.get("html", "")
                        # 表格以 HTML 形式嵌入 Markdown
                        parts.append(_clean_table_html(html))
        elif sub_type == BlockType.TABLE_CAPTION:
            caption_text = _lines_to_text(sub_block)
            parts.insert(0, f"**{caption_text}**")

    return "\n\n".join(parts)


def _list_block_to_markdown(block: dict, indent_level: int = 0) -> str:
    """将列表 block 转为 Markdown 列表"""
    lines = []
    indent = "  " * indent_level
    is_ordered = block.get("attribute", "unordered") == "ordered"
    start_num = block.get("start", 1)

    for idx, sub_block in enumerate(block.get("blocks", [])):
        sub_type = sub_block.get("type", "")
        if sub_type == BlockType.LIST:
            # 嵌套列表
            lines.append(_list_block_to_markdown(sub_block, indent_level + 1))
        else:
            text = _lines_to_text(sub_block)
            if is_ordered:
                lines.append(f"{indent}{start_num + idx}. {text}")
            else:
                lines.append(f"{indent}- {text}")

    return "\n".join(lines)


def _extract_caption_from_block(block: dict) -> str:
    """从包含 caption 子 block 的 visual block 中提取 caption 文本"""
    for sub_block in block.get("blocks", []):
        sub_type = sub_block.get("type", "")
        if sub_type in [BlockType.IMAGE_CAPTION, BlockType.TABLE_CAPTION,
                        BlockType.CHART_CAPTION, BlockType.CAPTION]:
            return _lines_to_text(sub_block)
    return ""


def _clean_table_html(html: str) -> str:
    """清洗表格 HTML，只保留结构信息 (colspan/rowspan)"""
    if not html:
        return ""

    # 保留的表格结构属性
    preserved = {'colspan', 'rowspan'}

    def clean_tag(match):
        full_tag = match.group(0)
        tag_name = match.group(1).lower()
        is_self_closing = full_tag.rstrip().endswith('/>')

        kept_attrs = []
        attr_pattern = r'(\w+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|(\S+))|(\w+)(?=\s|>|/>)'
        for attr_match in re.finditer(attr_pattern, full_tag):
            if attr_match.group(5):
                continue
            attr_name = (attr_match.group(1) or "").lower()
            attr_value = attr_match.group(2) or attr_match.group(3) or attr_match.group(4) or ""
            if attr_name in preserved:
                kept_attrs.append(f'{attr_name}="{attr_value}"')

        attrs_str = ' ' + ' '.join(kept_attrs) if kept_attrs else ''
        if is_self_closing:
            return f'<{tag_name}{attrs_str}/>'
        return f'<{tag_name}{attrs_str}>'

    tag_pattern = r'<(\w+)(?:\s+[^>]*)?\s*/?>'
    return re.sub(tag_pattern, clean_tag, html)


def _make_content_list(pdf_info: list[dict], img_bucket_path: str = "") -> list[dict]:
    """生成 content_list (按阅读顺序的内容列表)"""
    content_list = []

    for page_info in pdf_info:
        page_idx = page_info.get("page_idx", 0)
        blocks = page_info.get("preproc_blocks", [])

        for block in blocks:
            item = _block_to_content_item(block, page_idx, img_bucket_path)
            if item:
                content_list.append(item)

    return content_list


def _block_to_content_item(block: dict, page_idx: int, img_bucket_path: str) -> dict | None:
    """将 block 转为 content_list 的一项"""
    block_type = block.get("type", "")
    bbox = block.get("bbox", [0, 0, 0, 0])
    text = _lines_to_text(block)

    if not text and block_type not in [BlockType.IMAGE, BlockType.TABLE, BlockType.CHART, BlockType.SEAL]:
        return None

    item = {
        "type": block_type,
        "text": text,
        "bbox": bbox,
        "page_idx": page_idx,
    }

    # 特殊类型处理
    if block_type == BlockType.TABLE:
        item["html"] = _extract_table_html(block)
    elif block_type in [BlockType.IMAGE, BlockType.CHART, BlockType.SEAL]:
        item["image_path"] = _extract_image_path(block)

    return item


def _make_content_list_v2(pdf_info: list[dict], img_bucket_path: str = "") -> list[dict]:
    """生成 content_list_v2 (增强版内容列表，包含更多元数据)"""
    content_list = []

    for page_info in pdf_info:
        page_idx = page_info.get("page_idx", 0)
        page_size = page_info.get("page_size", [0, 0])
        blocks = page_info.get("preproc_blocks", [])

        for block in blocks:
            item = _block_to_content_item_v2(block, page_idx, page_size, img_bucket_path)
            if item:
                content_list.append(item)

    return content_list


def _block_to_content_item_v2(
    block: dict,
    page_idx: int,
    page_size: list,
    img_bucket_path: str,
) -> dict | None:
    """将 block 转为 content_list_v2 的一项 (增强版)"""
    base_item = _block_to_content_item(block, page_idx, img_bucket_path)
    if base_item is None:
        return None

    # 添加 V2 增强字段
    block_type = block.get("type", "")
    base_item["page_size"] = page_size
    base_item["block_type"] = _map_block_type_to_v2(block_type)
    base_item["level"] = block.get("level")

    # spans 详情
    spans_detail = []
    for line in block.get("lines", []):
        for span in line.get("spans", []):
            spans_detail.append({
                "type": span.get("type", ""),
                "content": span.get("content", ""),
            })
    base_item["spans"] = spans_detail

    return base_item


def _map_block_type_to_v2(block_type: str) -> str:
    """将 BlockType 映射为 ContentTypeV2"""
    mapping = {
        BlockType.TEXT: ContentTypeV2.PARAGRAPH,
        BlockType.TITLE: ContentTypeV2.TITLE,
        BlockType.IMAGE: ContentTypeV2.IMAGE,
        BlockType.TABLE: ContentTypeV2.TABLE,
        BlockType.CHART: ContentTypeV2.CHART,
        BlockType.INTERLINE_EQUATION: ContentTypeV2.EQUATION_INTERLINE,
        BlockType.LIST: ContentTypeV2.LIST,
        BlockType.HEADER: ContentTypeV2.PAGE_HEADER,
        BlockType.FOOTER: ContentTypeV2.PAGE_FOOTER,
        BlockType.PAGE_NUMBER: ContentTypeV2.PAGE_NUMBER,
    }
    return mapping.get(block_type, block_type)


def _extract_table_html(block: dict) -> str:
    """从表格 block 中提取 HTML"""
    for sub_block in block.get("blocks", []):
        for line in sub_block.get("lines", []):
            for span in line.get("spans", []):
                if span.get("type") == ContentType.TABLE:
                    return span.get("html", "")
    return ""


def _extract_image_path(block: dict) -> str:
    """从图片 block 中提取图片路径"""
    for sub_block in block.get("blocks", []):
        for line in sub_block.get("lines", []):
            for span in line.get("spans", []):
                if span.get("type") == ContentType.IMAGE:
                    return span.get("image_path", "")
    return ""

# Copyright (c) Opendatalab. All rights reserved.
"""
RAG 后端 MagicModel — 将 Azure DI + Dify 的结果归类为结构化 Block。

类似于 pipeline 和 office 的 MagicModel，负责:
1. 解析 Azure DI 返回的页面数据
2. 将检测到的元素归类为 BlockType (TEXT, TITLE, IMAGE, TABLE, CHART 等)
3. 关联 Dify 增强后的图片描述和表格优化结果
4. 按阅读顺序排列 blocks
"""
import re
from typing import Literal, Optional

from loguru import logger

from mineru.utils.enum_class import BlockType, ContentType


# ── Azure DI 段落角色到 BlockType 的映射 ─────────────────────
AZURE_ROLE_TO_BLOCK_TYPE = {
    "title": BlockType.TITLE,
    "sectionHeading": BlockType.TITLE,
    "pageHeader": BlockType.HEADER,
    "pageFooter": BlockType.FOOTER,
    "pageNumber": BlockType.PAGE_NUMBER,
    "footnote": BlockType.PAGE_FOOTNOTE,
    "formulaBlock": BlockType.INTERLINE_EQUATION,
}


class RAGMagicModel:
    """RAG 后端的 MagicModel — 将原始分析结果分类整理为标准 Block 结构"""

    def __init__(
        self,
        page_analysis: dict,              # 单页的 Azure DI 分析结果
        dify_image_results: Optional[list] = None,  # Dify 图片增强结果
        dify_table_results: Optional[list] = None,  # Dify 表格优化结果
        page_number: int = 0,
        page_width: int = 0,
        page_height: int = 0,
    ):
        self.page_number = page_number
        self.page_width = page_width
        self.page_height = page_height
        self.dify_image_map = self._build_dify_image_map(dify_image_results or [])
        self.dify_table_map = self._build_dify_table_map(dify_table_results or [])

        # 分类容器
        self.text_blocks: list[dict] = []
        self.title_blocks: list[dict] = []
        self.image_blocks: list[dict] = []
        self.table_blocks: list[dict] = []
        self.chart_blocks: list[dict] = []
        self.equation_blocks: list[dict] = []
        self.list_blocks: list[dict] = []
        self.discarded_blocks: list[dict] = []
        self.all_spans: list[dict] = []

        self._process(page_analysis)

    @staticmethod
    def _build_dify_image_map(dify_results: list) -> dict:
        """构建 image_key → DifyImageResult 的映射"""
        return {r.image_key: r for r in dify_results if hasattr(r, 'image_key')}

    @staticmethod
    def _build_dify_table_map(dify_results: list) -> dict:
        """构建 (page_number, table_index) → DifyTableResult 的映射"""
        return {
            (r.page_number, r.table_index): r
            for r in dify_results
            if hasattr(r, 'table_index')
        }

    def _process(self, page_analysis: dict) -> None:
        """主导处理流程 — 将 Azure DI 结果按元素类型分类处理"""
        block_index = 0

        # 1. 处理段落
        for para in page_analysis.get("paragraphs", []):
            block = self._process_paragraph(para, block_index)
            if block:
                self._classify_block(block)
            block_index += 1

        # 2. 处理表格
        for idx, table in enumerate(page_analysis.get("tables", [])):
            block = self._process_table(table, idx, block_index)
            if block:
                self._classify_block(block)
            block_index += 1

        # 3. 处理图片/图表区域
        for idx, figure in enumerate(page_analysis.get("figures", [])):
            block = self._process_figure(figure, idx, block_index)
            if block:
                self._classify_block(block)
            block_index += 1

        # 4. 按 bbox y 坐标排列 blocks (模拟阅读顺序)
        self._sort_blocks_by_reading_order()

    def _process_paragraph(self, para: dict, index: int) -> Optional[dict]:
        """处理单个段落"""
        content = para.get("content", "").strip()
        if not content:
            return None

        role = para.get("role", "").lower()
        bbox = para.get("bbox") or [0, 0, 0, 0]

        # 检查是否为公式
        if role == "formulaBlock" or self._is_formula_content(content):
            return {
                "type": BlockType.INTERLINE_EQUATION,
                "lines": [{"spans": [{"type": ContentType.INTERLINE_EQUATION, "content": self._clean_formula(content)}]}],
                "bbox": bbox,
                "index": index,
            }

        # 检查是否为列表项
        if self._is_list_item(content):
            return self._build_list_block(content, bbox, index)

        # 普通文本/标题段落
        block_type = AZURE_ROLE_TO_BLOCK_TYPE.get(role, BlockType.TEXT)
        # 自动检测标题（没有显式 role 但格式像标题的）
        if block_type == BlockType.TEXT and self._looks_like_title(content):
            block_type = BlockType.TITLE

        return {
            "type": block_type,
            "lines": [{"spans": self._parse_content_spans(content)}],
            "bbox": bbox,
            "index": index,
        }

    def _process_table(self, table: dict, table_idx: int, index: int) -> Optional[dict]:
        """处理单个表格 — 合并 Dify 优化结果"""
        bbox = self._get_table_bbox(table)
        table_html = table.get("table_html", "")
        caption_text = table.get("caption", "")

        # 尝试获取 Dify 优化结果
        dify_key = (self.page_number, table_idx)
        dify_result = self.dify_table_map.get(dify_key)
        if dify_result:
            optimized_html = dify_result.optimized_html or table_html
            optimized_md = dify_result.optimized_markdown
            if optimized_md:
                caption_text = optimized_md
        else:
            optimized_html = table_html
            optimized_md = ""

        return {
            "type": BlockType.TABLE,
            "blocks": [
                {
                    "type": BlockType.TABLE_BODY,
                    "lines": [{"spans": [{"type": ContentType.TABLE, "html": optimized_html}]}],
                    "bbox": bbox,
                }
            ],
            "bbox": bbox,
            "index": index,
            "_caption": caption_text,
            "_dify_enhanced": dify_result is not None,
        }

    def _process_figure(self, figure: dict, figure_idx: int, index: int) -> Optional[dict]:
        """处理单个图片/图表区域 — 合并 Dify 增强描述"""
        bbox = figure.get("bbox") or [0, 0, 0, 0]
        caption_text = figure.get("caption", "")

        # 从 caption 内容判断是 chart 还是 image
        is_chart = self._looks_like_chart(caption_text)

        # 尝试获取 Dify 增强结果（按 page + figure index 匹配）
        dify_description = ""
        for key, dify_result in self.dify_image_map.items():
            if hasattr(dify_result, 'page_number') and dify_result.page_number == self.page_number:
                # 根据 bbox 面积匹配
                if self._bbox_near(dify_result.bbox, bbox):
                    dify_description = dify_result.description
                    is_chart = dify_result.category == "chart"
                    break

        block_type = BlockType.CHART if is_chart else BlockType.IMAGE
        body_type = BlockType.CHART_BODY if is_chart else BlockType.IMAGE_BODY
        caption_type = BlockType.CHART_CAPTION if is_chart else BlockType.IMAGE_CAPTION

        blocks = [
            {
                "type": body_type,
                "lines": [{"spans": [{
                    "type": ContentType.IMAGE,
                    "image_path": f"figure_{figure_idx}_{self.page_number}.png",
                }]}],
                "bbox": bbox,
            }
        ]

        # Dify 增强描述作为 caption
        description_text = dify_description or caption_text
        if description_text:
            blocks.append({
                "type": caption_type,
                "lines": [{"spans": self._parse_content_spans(description_text)}],
                "bbox": bbox,
            })

        return {
            "type": block_type,
            "blocks": blocks,
            "bbox": bbox,
            "index": index,
            "_dify_enhanced": bool(dify_description),
        }

    def _parse_content_spans(self, text: str) -> list[dict]:
        """将文本解析为 spans，识别行内公式"""
        if not text:
            return [{"type": ContentType.TEXT, "content": ""}]

        spans = []
        # 匹配 $...$ (行内公式) 和 $$...$$ (行间公式)
        pattern = r'(\$\$?)(.+?)\1'
        last_end = 0

        for match in re.finditer(pattern, text):
            # 公式前的普通文本
            if match.start() > last_end:
                spans.append({
                    "type": ContentType.TEXT,
                    "content": text[last_end:match.start()],
                })

            delimiter = match.group(1)
            formula = match.group(2)
            if delimiter == "$$":
                spans.append({
                    "type": ContentType.INTERLINE_EQUATION,
                    "content": formula.strip(),
                })
            else:
                spans.append({
                    "type": ContentType.INLINE_EQUATION,
                    "content": formula.strip(),
                })
            last_end = match.end()

        # 剩余文本
        if last_end < len(text):
            spans.append({
                "type": ContentType.TEXT,
                "content": text[last_end:],
            })

        return spans if spans else [{"type": ContentType.TEXT, "content": text}]

    def _classify_block(self, block: dict) -> None:
        """将 block 归类到对应列表"""
        block_type = block.get("type", "")

        if block_type in [BlockType.HEADER, BlockType.FOOTER, BlockType.PAGE_NUMBER, BlockType.PAGE_FOOTNOTE]:
            self.discarded_blocks.append(block)
        elif block_type == BlockType.TITLE:
            self.title_blocks.append(block)
        elif block_type in [BlockType.IMAGE, BlockType.IMAGE_BODY, BlockType.IMAGE_CAPTION, BlockType.IMAGE_FOOTNOTE]:
            self.image_blocks.append(block)
        elif block_type in [BlockType.TABLE, BlockType.TABLE_BODY, BlockType.TABLE_CAPTION, BlockType.TABLE_FOOTNOTE]:
            self.table_blocks.append(block)
        elif block_type in [BlockType.CHART, BlockType.CHART_BODY, BlockType.CHART_CAPTION]:
            self.chart_blocks.append(block)
        elif block_type == BlockType.INTERLINE_EQUATION:
            self.equation_blocks.append(block)
        elif block_type == BlockType.LIST:
            self.list_blocks.append(block)
        else:
            self.text_blocks.append(block)

    def _sort_blocks_by_reading_order(self) -> None:
        """按 bbox y 坐标对所有 block 列表进行阅读顺序排序"""
        def sort_key(b):
            bbox = b.get("bbox", [0, 0, 0, 0])
            return (bbox[1], bbox[0])  # y 坐标优先，x 次之

        for attr in [
            "text_blocks", "title_blocks", "image_blocks",
            "table_blocks", "chart_blocks", "equation_blocks",
            "list_blocks", "discarded_blocks",
        ]:
            blocks = getattr(self, attr, [])
            blocks.sort(key=sort_key)

    # ── 辅助判断方法 ──────────────────────────────────────────

    @staticmethod
    def _is_formula_content(text: str) -> bool:
        """判断文本是否为公式内容"""
        return bool(re.match(r'^\s*(\$\$|\\\[)', text.strip()))

    @staticmethod
    def _clean_formula(text: str) -> str:
        """清理公式的 LaTeX 定界符"""
        latex = text.strip()
        for prefix, suffix in [("$$", "$$"), ("\\[", "\\]"), ("\\(", "\\)")]:
            if latex.startswith(prefix) and latex.endswith(suffix):
                return latex[len(prefix):-len(suffix)].strip()
        return latex

    @staticmethod
    def _is_list_item(text: str) -> bool:
        """判断文本是否为列表项"""
        return bool(re.match(r'^\s*[-•·*•]\s|^\s*\d+[.)]\s', text.strip()))

    @staticmethod
    def _looks_like_title(text: str) -> bool:
        """自动判断文本是否像标题"""
        t = text.strip()
        if len(t) > 60:
            return False
        # 以数字编号开头 (如 "1.1 概述")
        if re.match(r'^\d+(\.\d+)*\s+\S', t):
            return True
        # 全部加粗或特定关键词模式
        return False

    @staticmethod
    def _looks_like_chart(caption: str) -> bool:
        """根据说明文字判断是否为图表"""
        chart_keywords = ["chart", "图表", "折线图", "柱状图", "饼图", "散点图",
                          "趋势图", "曲线", "bar chart", "pie chart", "line chart"]
        return any(kw in caption.lower() for kw in chart_keywords)

    @staticmethod
    def _bbox_near(bbox_a, bbox_b, threshold=50) -> bool:
        """判断两个 bbox 是否接近"""
        if not bbox_a or not bbox_b:
            return False
        # IoU 简单近似
        ax0, ay0, ax1, ay1 = bbox_a
        bx0, by0, bx1, by1 = bbox_b
        overlap_x = max(0, min(ax1, bx1) - max(ax0, bx0))
        overlap_y = max(0, min(ay1, by1) - max(ay0, by0))
        if overlap_x <= 0 and overlap_y <= 0:
            return abs(ay0 - by0) < threshold and abs(ax0 - bx0) < threshold
        return True

    @staticmethod
    def _get_table_bbox(table: dict) -> list[int]:
        """从表格的 cells 中计算整体 bbox"""
        cells = table.get("cells", [])
        if not cells:
            return [0, 0, 0, 0]
        x0 = min(c.get("bbox", [0, 0, 0, 0])[0] for c in cells if c.get("bbox"))
        y0 = min(c.get("bbox", [0, 0, 0, 0])[1] for c in cells if c.get("bbox"))
        x1 = max(c.get("bbox", [0, 0, 0, 0])[2] for c in cells if c.get("bbox"))
        y1 = max(c.get("bbox", [0, 0, 0, 0])[3] for c in cells if c.get("bbox"))
        return [x0, y0, x1, y1]

    def _build_list_block(self, content: str, bbox: list[int], index: int) -> dict:
        """构建列表类型的 block"""
        # 简单列表项解析
        items = re.split(r'\n(?=[-•·*•]\s|\d+[.)]\s)', content.strip())
        list_blocks = []
        for item in items:
            item = re.sub(r'^[-•·*•]\s*|\d+[.)]\s*', '', item.strip(), count=1)
            list_blocks.append({
                "type": BlockType.TEXT,
                "lines": [{"spans": self._parse_content_spans(item)}],
            })

        return {
            "type": BlockType.LIST,
            "blocks": list_blocks,
            "bbox": bbox,
            "index": index,
        }

    # ── 公共访问方法 ──────────────────────────────────────────

    def get_text_blocks(self) -> list[dict]:
        return self.text_blocks

    def get_title_blocks(self) -> list[dict]:
        return self.title_blocks

    def get_image_blocks(self) -> list[dict]:
        return self.image_blocks

    def get_table_blocks(self) -> list[dict]:
        return self.table_blocks

    def get_chart_blocks(self) -> list[dict]:
        return self.chart_blocks

    def get_equation_blocks(self) -> list[dict]:
        return self.equation_blocks

    def get_list_blocks(self) -> list[dict]:
        return self.list_blocks

    def get_discarded_blocks(self) -> list[dict]:
        return self.discarded_blocks

    def get_preproc_blocks(self) -> list[dict]:
        """获取按阅读顺序排列的所有 blocks 列表"""
        all_blocks = (
            self.title_blocks +
            self.text_blocks +
            self.image_blocks +
            self.table_blocks +
            self.chart_blocks +
            self.equation_blocks +
            self.list_blocks
        )
        # 按页面 y 坐标排序确保阅读顺序
        all_blocks.sort(key=lambda b: b.get("bbox", [0, 0, 0, 0])[1])
        return all_blocks

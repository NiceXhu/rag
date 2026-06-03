# Copyright (c) Opendatalab. All rights reserved.
"""
图片相关性过滤 — 在送 Dify 增强之前识别并跳过无意义图片。

问题场景:
- PPT 转 PDF 中的背景图片、角标图标、模板装饰元素
- 这些图片没有信息量, Dify 生成的描述反而污染 Markdown 输出

过滤策略 (多级):
1. 尺寸过滤 — 过小的图片 (图标/角标)
2. 位置过滤 — 页面边缘、角落的图片 (模板元素)
3. 跨页重复检测 — 同一图片出现在多页 (Logo/模板背景)
4. 宽高比异常 — 极窄或极扁的装饰分割线
5. 上下文缺失 — 周围文本不提及该图片

过滤后的图片仍保留在输出中, 但不调用 Dify 生成描述。
"""
import hashlib
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger


# ── 阈值配置 ──────────────────────────────────────────────

# 尺寸: 小于此面积的图片视为装饰 (inch²)
MIN_MEANINGFUL_AREA = 0.5          # 0.5 sq inch ≈ 0.7" × 0.7"

# 尺寸: 小于此面积的绝对过滤 (inch²)
ABSOLUTE_MIN_AREA = 0.1            # 0.1 sq inch — 一定是图标

# 位置: 边距比例 — 图片中心距页面边缘 < 此比例视为角落元素
EDGE_MARGIN_RATIO = 0.08           # 8% 以内

# 宽高比: 超出此范围的视为装饰分割线
MIN_ASPECT_RATIO = 0.15            # 高/宽
MAX_ASPECT_RATIO = 12.0            # 高/宽 (放宽以允许细长箭头/标注)

# 跨页重复: 相同签名出现 ≥ 此次数视为模板元素
TEMPLATE_REPETITION_THRESHOLD = 2  # 出现 ≥ 2 页

# 上下文: 周围文本 (caption + 前后段落) 至少包含此长度
MIN_CONTEXT_LENGTH = 10            # 字符

# 背景检测: 图片覆盖页面比例 > 此值 → 可能是背景
BACKGROUND_COVERAGE_RATIO = 0.65   # 65% 的页面面积

# 文本重叠: 超过此数量的 text paragraph 与图片重叠 → 背景
TEXT_OVERLAP_COUNT = 3             # ≥ 3 个文本块重叠

# 内容孤立: 周围文本的引用关键词数量 < 此数 → 内容孤立
MIN_CONTEXT_REFERENCES = 1

# ── 截图检测阈值 ────────────────────────────────────────
# 截图特征: 覆盖率高 + 文字重叠多 = 可能是 UI 截图而非背景
SCREENSHOT_MIN_COVERAGE = 0.35     # 占页面 ≥ 35%
SCREENSHOT_MIN_OVERLAP = 4         # 重叠文字块 ≥ 4 段
SCREENSHOT_MIN_AREA = 2.0          # 最小面积 (inch²) — 排除小图标

# ── PPT 模式 (RAG_PPT_MODE=true 时启用) ────────────────
PPT_MODE_OVERLAP_THRESHOLD = 10    # R6 阈值提高到 10
PPT_MODE_MIN_AREA = 0.2            # 更低的装饰面积阈值


# ══════════════════════════════════════════════════════════
# 可插拔无关内容检测器框架
# ══════════════════════════════════════════════════════════

@dataclass
class ArtifactDetector:
    """
    无关内容检测器。

    每个检测器有一组 regex 模式和一个类别名称。
    新增检测器时只需创建实例并注册到 ARTIFACT_DETECTORS 列表。
    """
    category: str                              # 类别名 (用于日志和跳过原因)
    label: str                                 # 人类可读标签
    patterns: list[str] = field(default_factory=list)       # regex 模式列表
    keywords: list[str] = field(default_factory=list)      # 关键词 (忽略大小写子串匹配)
    min_matches: int = 2                       # 最小匹配数 (regex + keyword 合计)

    def detect(self, text: str) -> list[str]:
        """
        扫描文本, 返回匹配到的模式描述列表。

        Args:
            text: OCR 文本 (已 lower)

        Returns:
            ["pattern1 desc", "keyword: xxx", ...]
        """
        matched = []

        for pattern in self.patterns:
            if re.search(pattern, text, re.IGNORECASE):
                # 生成简短描述
                desc = pattern.replace(r'\b', '').replace(r'\s+', ' ')[:60]
                matched.append(f"re:{desc}")

        for kw in self.keywords:
            if kw.lower() in text:
                matched.append(f"kw:{kw}")

        return matched


# ── 内置检测器 ────────────────────────────────────────────

BUILTIN_DETECTORS = [
    ArtifactDetector(
        category="email",
        label="邮件截图/签名",
        patterns=[
            r'\b(from|to|cc|bcc|subject|sent|date)\s*:',
            r'\b(re|fw|fwd)\s*:',
            r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}',
        ],
        keywords=["regards", "cheers", "best regards", "sincerely", "dear", "hi team",
                   "inbox", "outbox", "sent mail", "forwarded message"],
        min_matches=2,
    ),
    ArtifactDetector(
        category="chat",
        label="聊天记录/消息",
        patterns=[
            r'\b(message|chat|conversation|thread)\b',
            r'\b(typing|online|offline|last seen)\b',
            r'\b(read|delivered|sent)\s+\d{1,2}:\d{2}',
        ],
        keywords=["text message", "group chat", "iMessage", "WhatsApp", "Telegram",
                   "typing...", "today", "yesterday"],
        min_matches=2,
    ),
    ArtifactDetector(
        category="meeting",
        label="会议邀请/日历",
        patterns=[
            r'\b(meeting|invitation|calendar|rsvp|reminder)\b',
            r'\b(join|attend|schedule|reschedule|cancel)\s+(the\s+)?(meeting|call)',
            r'\b(zoom|teams|meet|webex|google meet)\b',
        ],
        keywords=["meeting id", "passcode", "dial", "join meeting", "recurring",
                   "organizer", "attendees", "accepted", "declined", "tentative"],
        min_matches=2,
    ),
    ArtifactDetector(
        category="watermark",
        label="水印/标注/印章",
        patterns=[
            r'\b(confidential|draft|internal|do not distribute|sample)\b',
            r'\b(copyright|all rights reserved|proprietary)\b',
            r'\b(approved|reviewed|pending|final|version\s*\d)\b',
        ],
        keywords=["top secret", "for internal use only", "draft", "do not copy",
                   "preliminary", "under review"],
        min_matches=1,  # 水印通常一个词就够了
    ),
    ArtifactDetector(
        category="social_media",
        label="社交媒体截图",
        patterns=[
            r'\b(follow|followers|following|tweet|retweet|like|share|comment)\b',
            r'\b(post|profile|timeline|news feed|notification)\b',
            r'\b(reply|repost|reposted)\b',
        ],
        keywords=["facebook", "twitter", "instagram", "linkedin", "tiktok",
                   "snapchat", "weibo", "wechat moment"],
        min_matches=2,
    ),
    ArtifactDetector(
        category="code_terminal",
        label="代码/终端截图",
        patterns=[
            r'\b(error|exception|traceback|stack trace)\b',
            r'\b(warning|debug|info|fatal)\s*[:\]]',
            r'[~/]\$\s',  # shell prompt
            r'\b(import\s+\w+|from\s+\w+\s+import)\b',
        ],
        keywords=["terminal", "console", "command line", "syntax error",
                   "runtime error", "compilation error", "exit code"],
        min_matches=2,
    ),
    ArtifactDetector(
        category="advertisement",
        label="广告/推广内容",
        patterns=[
            r'\b(buy now|shop now|limited time|offer ends|discount)\b',
            r'\b(free trial|subscribe|pricing|upgrade|premium)\b',
            r'\b(sponsored|promoted|advertisement)\b',
        ],
        keywords=["click here", "learn more", "get started", "sign up now",
                   "special offer", "exclusive deal", "save", "money back"],
        min_matches=2,
    ),
]

# 全局检测器注册表 (内置 + 用户自定义)
ARTIFACT_DETECTORS: list[ArtifactDetector] = list(BUILTIN_DETECTORS)

# 全局最低匹配阈值
ARTIFACT_PATTERN_MIN_MATCHES = 2


def load_custom_detectors_from_config() -> list[ArtifactDetector]:
    """
    从 ~/mineru.json 加载用户自定义的无关内容检测器。

    配置格式:
    {
      "rag_artifact_detectors": [
        {
          "category": "my_custom",
          "label": "自定义无关内容",
          "patterns": ["regex1", "regex2"],
          "keywords": ["keyword1", "keyword2"],
          "min_matches": 2
        }
      ]
    }

    也支持通过环境变量快速添加:
      MINERU_ARTIFACT_KEYWORDS="发票,报销,审批单"
      MINERU_ARTIFACT_PATTERNS="发票号.*:,审批人:"
    """
    custom = []

    # ── 方式1: ~/mineru.json 配置 ──
    try:
        from mineru.utils.config_reader import read_config
        config = read_config()
        if config:
            detector_configs = config.get("rag_artifact_detectors", [])
            for dc in detector_configs:
                detector = ArtifactDetector(
                    category=dc.get("category", "custom"),
                    label=dc.get("label", "自定义检测器"),
                    patterns=dc.get("patterns", []),
                    keywords=dc.get("keywords", []),
                    min_matches=dc.get("min_matches",
                                       ARTIFACT_PATTERN_MIN_MATCHES),
                )
                custom.append(detector)
                logger.debug(f"加载自定义检测器: {detector.category} ({detector.label})")
    except Exception as e:
        logger.debug(f"从 config 加载自定义检测器失败 (可忽略): {e}")

    # ── 方式2: 环境变量快速添加 ──
    env_keywords = os.getenv("MINERU_ARTIFACT_KEYWORDS", "")
    env_patterns = os.getenv("MINERU_ARTIFACT_PATTERNS", "")

    if env_keywords or env_patterns:
        kw_list = [k.strip() for k in env_keywords.split(",") if k.strip()]
        pat_list = [p.strip() for p in env_patterns.split(",") if p.strip()]
        if kw_list or pat_list:
            custom.append(ArtifactDetector(
                category="env_custom",
                label="环境变量自定义",
                patterns=pat_list,
                keywords=kw_list,
                min_matches=1,
            ))
            logger.debug(f"从环境变量加载检测器: {len(kw_list)} keywords, {len(pat_list)} patterns")

    return custom


def reload_detectors() -> list[ArtifactDetector]:
    """
    重新加载所有检测器 (内置 + 自定义)。

    每次 Pipeline 启动时调用, 确保使用最新的配置。
    """
    global ARTIFACT_DETECTORS
    custom = load_custom_detectors_from_config()
    ARTIFACT_DETECTORS = list(BUILTIN_DETECTORS) + custom
    logger.debug(
        f"检测器已加载: {len(BUILTIN_DETECTORS)} 内置 + {len(custom)} 自定义 = {len(ARTIFACT_DETECTORS)} 总"
    )
    return ARTIFACT_DETECTORS


@dataclass
class ImageAssessment:
    """单张图片的评估结果"""
    image_key: str
    page_number: int
    bbox: Optional[list[float]] = None
    area_sq_inch: float = 0.0
    aspect_ratio: float = 1.0
    is_edge_position: bool = False
    is_repeated_template: bool = False
    has_context: bool = False
    text_overlap_count: int = 0       # 与该图重叠的段落数
    coverage_ratio: float = 0.0       # 占页面比例
    artifact_matches: list[str] = field(default_factory=list)  # 匹配到的检测器 category
    context_ref_count: int = 0        # 上下文引用数
    should_skip: bool = False
    skip_reason: str = ""
    signature: str = ""            # 用于跨页重复检测的 hash


@dataclass
class FilterResult:
    """批量过滤结果"""
    total: int = 0
    kept: int = 0                  # 值得增强的
    skipped: int = 0               # 跳过的
    skipped_reasons: Counter = field(default_factory=Counter)
    assessments: list[ImageAssessment] = field(default_factory=list)


# ── 计算辅助函数 ──────────────────────────────────────────

def _calc_area(bbox: Optional[list[float]]) -> float:
    """计算 bbox 面积 (inch²)"""
    if not bbox or len(bbox) < 4:
        return 0.0
    w = max(bbox[2] - bbox[0], 0)
    h = max(bbox[3] - bbox[1], 0)
    return w * h


def _calc_aspect_ratio(bbox: Optional[list[float]]) -> float:
    """计算宽高比 (height / width)"""
    if not bbox or len(bbox) < 4:
        return 1.0
    w = max(bbox[2] - bbox[0], 0.01)
    h = max(bbox[3] - bbox[1], 0.01)
    return h / w


def _is_edge_position(
    bbox: Optional[list[float]],
    page_width: float,
    page_height: float,
) -> bool:
    """判断图片是否在页面边缘/角落位置"""
    if not bbox or len(bbox) < 4 or page_width <= 0 or page_height <= 0:
        return False

    center_x = (bbox[0] + bbox[2]) / 2
    center_y = (bbox[1] + bbox[3]) / 2

    margin_x = page_width * EDGE_MARGIN_RATIO
    margin_y = page_height * EDGE_MARGIN_RATIO

    # 在边缘 (左/右/上/下) 或在角落
    return (
        center_x < margin_x or center_x > page_width - margin_x or
        center_y < margin_y or center_y > page_height - margin_y
    )


def _compute_signature(figure: dict) -> str:
    """
    计算图片签名 (用于跨页重复检测)。

    基于尺寸 + 宽高比 + 面积覆盖率，多维度降低签名碰撞。
    培训 PPT 中所有截图尺寸相同但宽高比和覆盖率可能不同。
    """
    bbox = figure.get("bbox") or [0, 0, 0, 0]
    w = max(bbox[2] - bbox[0], 0)
    h = max(bbox[3] - bbox[1], 0)

    if w == 0 or h == 0:
        return "empty"

    # 量化到 0.1 inch 精度
    w_bucket = round(w * 10) / 10
    h_bucket = round(h * 10) / 10
    # 宽高比 bucket (0.1 精度)
    aspect = round(h / w, 1)
    # 面积 bucket (0.5 sq inch 精度)
    area_bucket = round((w * h) / 0.5) * 0.5

    sig = f"{w_bucket:.1f}x{h_bucket:.1f}_a{aspect:.1f}_s{area_bucket:.1f}"
    return hashlib.md5(sig.encode()).hexdigest()[:8]


def _count_overlapping_paragraphs(
    figure_bbox: Optional[list[float]],
    paragraphs: list[dict],
) -> int:
    """
    统计有多少段落与图片区域重叠。

    背景图的特征: 文本是渲染在背景之上的,
    所以大量文本的 bbox 与图片 bbox 有交集。
    """
    if not figure_bbox or len(figure_bbox) < 4:
        return 0

    fx0, fy0, fx1, fy1 = figure_bbox
    count = 0

    for para in paragraphs:
        pb = para.get("bbox")
        if not pb or len(pb) < 4:
            continue
        px0, py0, px1, py1 = pb

        # 检查两个矩形是否有交集
        if px0 < fx1 and px1 > fx0 and py0 < fy1 and py1 > fy0:
            count += 1

    return count


def _calc_page_coverage(
    figure_bbox: Optional[list[float]],
    page_width: float,
    page_height: float,
) -> float:
    """
    计算图片占页面的比例。
    """
    if not figure_bbox or page_width <= 0 or page_height <= 0:
        return 0.0
    area = _calc_area(figure_bbox)
    page_area = page_width * page_height
    return area / page_area if page_area > 0 else 0.0


def _is_likely_screenshot(
    area: float,
    page_width: float,
    page_height: float,
    overlap_count: int,
    overlap_avg_len: float,
) -> bool:
    """
    检测图片是否可能是 UI 截图而非背景。

    截图特征:
    1. 覆盖率高 — 通常占据大部分 slide 面积
    2. 文字重叠多 — OCR 识别出大量 UI 文本
    3. 重叠文字短 — UI 标签/按钮文字而非正文段落
    4. 面积 ≥ 下限 — 排除小图标
    """
    if area < SCREENSHOT_MIN_AREA:
        return False
    if page_width <= 0 or page_height <= 0:
        return False
    coverage = area / (page_width * page_height)
    if coverage < SCREENSHOT_MIN_COVERAGE:
        return False
    if overlap_count < SCREENSHOT_MIN_OVERLAP:
        return False
    # 截图文字是短标签 (< 80 chars avg)，背景图文字是完整段落
    if overlap_avg_len > 80:
        return False
    return True


def _extract_figure_overlap_text(
    figure_bbox: Optional[list[float]],
    paragraphs: list[dict],
) -> str:
    """
    提取与图片区域重叠的所有 OCR 文本。

    用于对图片内容进行模式匹配 (如检测邮件截图)。
    """
    if not figure_bbox or len(figure_bbox) < 4:
        return ""

    fx0, fy0, fx1, fy1 = figure_bbox
    texts = []

    for para in paragraphs:
        pb = para.get("bbox")
        if not pb or len(pb) < 4:
            continue
        px0, py0, px1, py1 = pb

        # 段落与图片区域有交集
        if px0 < fx1 and px1 > fx0 and py0 < fy1 and py1 > fy0:
            texts.append(para.get("content", ""))

    return " ".join(texts)


def _detect_artifact_content(
    figure_bbox: Optional[list[float]],
    paragraphs: list[dict],
) -> tuple[bool, list[str]]:
    """
    检测图片区域内的文本是否包含嵌入物特征。

    扫描图片区域内的 OCR 文本,
    匹配邮件格式、聊天记录、水印等无关内容模式。

    Returns:
        (is_artifact, [matched_patterns])
    """
    overlap_text = _extract_figure_overlap_text(figure_bbox, paragraphs)
    if not overlap_text:
        return False, []

    text_lower = overlap_text.lower()
    all_matched = []
    triggered_detectors = []

    for detector in ARTIFACT_DETECTORS:
        det_matches = detector.detect(text_lower)
        if len(det_matches) >= detector.min_matches:
            all_matched.extend(det_matches)
            triggered_detectors.append(detector.category)

    is_artifact = len(triggered_detectors) > 0
    return is_artifact, triggered_detectors


def _count_context_references(
    figure: dict,
    context_text: str,
    paragraphs: list[dict],
    figure_bbox: Optional[list[float]],
) -> int:
    """
    统计周围上下文对图片的引用次数。

    包括:
    - caption 中有意义的描述词
    - 前后段落中的「如图」「Figure」「shown in」等引用
    - 图片附近段落的锚点/交叉引用
    """
    ref_count = 0

    # Caption 中的引号/描述词
    caption = figure.get("caption", "")
    if caption and len(caption.strip()) >= MIN_CONTEXT_LENGTH:
        ref_count += 1

    # 上下文引用词 (学术通用 + 培训PPT)
    ref_keywords = [
        r'如图', r'见图', r'如图所示', r'如下图', r'上图', r'下图',
        r'figure\s*\d', r'fig\.\s*\d', r'as shown', r'see figure',
        r'following (image|figure|chart|diagram|picture)',
        r'below', r'above',
        r'所示', r'参见图',
        # 培训 PPT 场景
        r'示例', r'示意', r'示意图', r'如下所示', r'参考下图',
        r'界面', r'窗口', r'对话框', r'菜单', r'按钮',
        r'screenshot', r'example', r'sample', r'demo',
        r'click', r'select', r'操作', r'步骤', r'配置',
    ]

    ctx_lower = context_text.lower()
    for kw in ref_keywords:
        if re.search(kw, ctx_lower):
            ref_count += 1

    # 图片附近段落的距离检测 (caption 通常在图片紧邻位置)
    if figure_bbox and len(figure_bbox) >= 4:
        fy1 = figure_bbox[3]  # 图片底部
        for para in paragraphs:
            pb = para.get("bbox")
            if not pb or len(pb) < 4:
                continue
            # 段落紧邻图片下方 (0.3 inch 以内)
            if 0 < pb[1] - fy1 < 0.3:
                content = para.get("content", "").lower()
                if any(kw.replace('\\', '') in content for kw in ref_keywords[:6]):
                    ref_count += 1

    return ref_count


def _has_context(
    figure: dict,
    page_paragraphs: list[dict],
    context_text: str,
) -> bool:
    """
    判断图片是否有上下文引用。

    检查:
    1. Azure DI 返回的 figure caption 是否有内容
    2. 周围文本是否提及图片 (中文「图」/「如」/「见」, 英文 Figure/see/shown)
    """
    # Caption 检查
    caption = figure.get("caption", "")
    if caption and len(caption.strip()) >= MIN_CONTEXT_LENGTH:
        return True

    # 周围文本检查
    if len(context_text.strip()) >= MIN_CONTEXT_LENGTH:
        context_lower = context_text.lower()
        # 多语言引用词 (学术 + 培训 PPT)
        ref_keywords = [
            "图", "如图", "见下图", "下表", "figure", "fig.", "shown",
            "illustrated", "depicted", "image", "chart", "diagram",
            "照片", "图示", "参见",
            # 培训 PPT 场景
            "示例", "示意", "示意", "如下", "截图", "界面", "窗口",
            "对话框", "菜单", "按钮", "操作", "步骤", "配置",
            "screenshot", "example", "sample", "demo", "click", "select",
        ]
        for kw in ref_keywords:
            if kw in context_lower:
                return True

    return False


# ── 主过滤逻辑 ──────────────────────────────────────────────

def assess_image_relevance(
    figure: dict,
    page_number: int,
    page_width: float = 0,
    page_height: float = 0,
    page_paragraphs: Optional[list[dict]] = None,
    context_text: str = "",
    template_signatures: Optional[set[str]] = None,
    fig_index: int = 0,
) -> ImageAssessment:
    """
    对单张图片进行多维度评估, 判断是否值得送 Dify 增强。

    Args:
        figure: Azure DI 返回的 figure dict
        page_number: 所在页
        page_width: 页面宽度 (inches)
        page_height: 页面高度 (inches)
        page_paragraphs: 页面段落
        context_text: 上下文文本
        template_signatures: 已识别为模板元素的签名集合
        fig_index: 图片在页面中的序号

    Returns:
        ImageAssessment 评估结果
    """
    bbox = figure.get("bbox")
    area = _calc_area(bbox)
    aspect = _calc_aspect_ratio(bbox)
    sig = _compute_signature(figure)
    key = f"fig_p{page_number}_{fig_index}"

    assessment = ImageAssessment(
        image_key=key,
        page_number=page_number,
        bbox=bbox,
        area_sq_inch=area,
        aspect_ratio=aspect,
        signature=sig,
    )

    # ── PPT 模式检测 ──
    ppt_mode = os.getenv("RAG_PPT_MODE", "").lower() in ("true", "1", "yes")
    _overlap_threshold = PPT_MODE_OVERLAP_THRESHOLD if ppt_mode else TEXT_OVERLAP_COUNT
    _min_area = PPT_MODE_MIN_AREA if ppt_mode else MIN_MEANINGFUL_AREA

    if ppt_mode:
        logger.debug(f"图片过滤: PPT 模式已启用 "
                     f"(overlap_threshold={_overlap_threshold}, min_area={_min_area})")

    # ── 规则 1: 绝对尺寸过滤 ──
    if area < ABSOLUTE_MIN_AREA:
        assessment.should_skip = True
        assessment.skip_reason = f"absolute_min_area ({area:.2f} < {ABSOLUTE_MIN_AREA} sq in)"
        return assessment

    # ── 规则 2: 尺寸 + 边缘位置 → 角标/图标 ──
    # PPT 模式下: 有上下文引用的小边缘元素放行 (可能是内容的标注箭头)
    if area < _min_area:
        is_edge = _is_edge_position(bbox, page_width, page_height)
        assessment.is_edge_position = is_edge

        if is_edge:
            # PPT 模式: 有 caption/引用则放行
            if ppt_mode and (
                (figure.get("caption") or "").strip()
                or _has_context(figure, page_paragraphs or [], context_text)
            ):
                pass  # 放行
            else:
                assessment.should_skip = True
                assessment.skip_reason = f"small_edge_element (area={area:.2f}, edge_pos)"
                return assessment

    # ── 预计算: 截图检测 (用于 R3/R6 保护) ──
    overlap_count = _count_overlapping_paragraphs(bbox, page_paragraphs or [])
    overlap_text = _extract_figure_overlap_text(bbox, page_paragraphs or [])
    overlap_words = overlap_text.split() if overlap_text else []
    overlap_avg_len = sum(len(w) for w in overlap_words) / max(len(overlap_words), 1) * 5

    is_screenshot = _is_likely_screenshot(
        area, page_width, page_height, overlap_count, overlap_avg_len,
    )
    if is_screenshot:
        assessment.text_overlap_count = overlap_count

    # ── 规则 3: 模板元素重复 ──
    # ★ 截图保护: 操作步骤截图内容不同但尺寸相同, 不是模板
    if template_signatures and sig in template_signatures and not is_screenshot:
        assessment.is_repeated_template = True
        assessment.should_skip = True
        assessment.skip_reason = f"template_repetition (sig={sig})"
        return assessment

    # ── 规则 4: 宽高比异常 ──
    if aspect < MIN_ASPECT_RATIO or aspect > MAX_ASPECT_RATIO:
        assessment.should_skip = True
        assessment.skip_reason = f"abnormal_aspect_ratio ({aspect:.2f})"
        return assessment

    # ── 规则 5: 无上下文引用 ──
    has_ctx = _has_context(figure, page_paragraphs or [], context_text)
    assessment.has_context = has_ctx

    if area < _min_area * 3 and not has_ctx:
        assessment.should_skip = True
        assessment.skip_reason = f"small_no_context (area={area:.2f}, no ref)"
        return assessment

    # ── 规则 6: 文本重叠 (背景检测) ──
    # ★ 截图保护: 截图 OCR 文字天然重叠, 不是背景信号
    if not is_screenshot:
        assessment.text_overlap_count = overlap_count
        if overlap_count >= _overlap_threshold:
            assessment.should_skip = True
            assessment.skip_reason = (
                f"text_overlap_bg ({overlap_count} paragraphs overlap → likely background)"
            )
            return assessment

    # ── 规则 7: 高页面覆盖率 ──
    coverage = _calc_page_coverage(bbox, page_width, page_height)
    assessment.coverage_ratio = coverage

    # ★ 截图保护: 截图覆盖率天然高, 用 R8 来判定而非 R7
    if not is_screenshot and coverage > BACKGROUND_COVERAGE_RATIO and not has_ctx:
        assessment.should_skip = True
        assessment.skip_reason = (
            f"high_coverage_bg (covers {coverage:.0%} of page, no context ref)"
        )
        return assessment

    # ── 规则 8: 嵌入物内容检测 ──
    is_artifact, artifact_matches = _detect_artifact_content(
        bbox, page_paragraphs or [],
    )
    assessment.artifact_matches = artifact_matches

    if is_artifact:
        # 截图 + 无关内容模式 → 确实是无价值截屏 (邮件/聊天记录)
        # 截图 + 无无关模式 → 是有价值内容 (操作界面/图表)
        if is_screenshot:
            logger.debug(
                f"  截图 {key}: 检测到嵌入物模式 {artifact_matches} → 过滤"
            )
        assessment.should_skip = True
        triggered_labels = []
        for det in ARTIFACT_DETECTORS:
            if det.category in artifact_matches:
                triggered_labels.append(det.label)
        assessment.skip_reason = (
            f"artifact_content ({', '.join(triggered_labels)})"
        )
        return assessment

    # ── 规则 9: 内容孤立检测 ──
    ref_count = _count_context_references(
        figure, context_text, page_paragraphs or [], bbox,
    )
    assessment.context_ref_count = ref_count

    if ref_count < MIN_CONTEXT_REFERENCES:
        if _min_area * 2 < area < _min_area * 20:
            assessment.should_skip = True
            assessment.skip_reason = (
                f"contextually_isolated (area={area:.1f}sq\", 0 refs → likely embedded artifact)"
            )
            return assessment

    # ── 截图通过过滤 — 记录日志 ──
    if is_screenshot:
        logger.debug(
            f"  截图 {key} 通过过滤: area={area:.1f}sq\", "
            f"coverage={coverage:.0%}, overlap={overlap_count}"
        )

    return assessment


def filter_images_for_enhancement(
    figures_by_page: dict[int, list[dict]],
    pages_data: list[dict],
) -> FilterResult:
    """
    对所有页面的图片进行批量过滤。

    两遍扫描:
    1. 第一遍: 收集所有图片签名, 检测跨页重复的模板元素
    2. 第二遍: 逐图评估, 使用第一遍检测出的模板签名

    Args:
        figures_by_page: {page_number: [figure_dict, ...]}
        pages_data: 页面数据列表

    Returns:
        FilterResult 包含评估详情
    """
    result = FilterResult()

    # ── 第一遍: 模板元素检测 ──
    all_signatures = []
    sig_to_keys: dict[str, list[str]] = defaultdict(list)

    for page_data in pages_data:
        page_num = page_data["page_number"]
        for fig_idx, figure in enumerate(page_data.get("figures", [])):
            if not figure.get("image_base64"):
                continue
            sig = _compute_signature(figure)
            key = f"fig_p{page_num}_{fig_idx}"
            all_signatures.append(sig)
            sig_to_keys[sig].append(key)

    # 识别重复出现的模板签名
    template_sigs: set[str] = set()
    sig_counter = Counter(all_signatures)
    for sig, count in sig_counter.items():
        if count >= TEMPLATE_REPETITION_THRESHOLD:
            template_sigs.add(sig)
            logger.debug(
                f"模板元素检测: sig={sig}, 出现 {count} 次 → 标记为装饰元素"
            )

    # ── 第二遍: 逐图评估 ──
    for page_data in pages_data:
        page_num = page_data["page_number"]
        page_width = page_data.get("width", 0)
        page_height = page_data.get("height", 0)
        paragraphs = page_data.get("paragraphs", [])
        context_text = " ".join(p.get("content", "") for p in paragraphs)
        tables_text = " ".join(
            t.get("caption", "") for t in page_data.get("tables", [])
        )

        for fig_idx, figure in enumerate(page_data.get("figures", [])):
            if not figure.get("image_base64"):
                continue

            result.total += 1
            assessment = assess_image_relevance(
                figure=figure,
                page_number=page_num,
                page_width=page_width,
                page_height=page_height,
                page_paragraphs=paragraphs,
                context_text=f"{context_text} {tables_text}",
                template_signatures=template_sigs,
                fig_index=fig_idx,
            )
            result.assessments.append(assessment)

            if assessment.should_skip:
                result.skipped += 1
                result.skipped_reasons[assessment.skip_reason] += 1
                # 标记 figure 为跳过
                figure["_skip_dify"] = True
                figure["_skip_reason"] = assessment.skip_reason
            else:
                result.kept += 1

    if result.skipped > 0:
        logger.info(
            f"图片过滤: {result.kept}/{result.total} 送 Dify, "
            f"{result.skipped} 跳过 — "
            + ", ".join(f"{r}: {c}" for r, c in result.skipped_reasons.most_common(5))
        )

    return result


# ── 集成入口 ──────────────────────────────────────────────

def apply_image_filter(pages_data: list[dict]) -> FilterResult:
    """
    对 pages_data 中的所有图片进行过滤。

    在 Dify 任务构建之前调用。被标记为 _skip_dify=True 的图片
    不会构建 Dify 任务, 但仍会保留在输出 Markdown 中 (无 LLM 描述)。

    Args:
        pages_data: 页面数据列表

    Returns:
        FilterResult 评估详情
    """
    # ★ 每次 Pipeline 启动时重新加载检测器 (支持热更新配置)
    reload_detectors()

    figures_by_page: dict[int, list[dict]] = defaultdict(list)
    for page_data in pages_data:
        page_num = page_data["page_number"]
        figures_by_page[page_num] = page_data.get("figures", [])

    return filter_images_for_enhancement(figures_by_page, pages_data)



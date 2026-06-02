# Copyright (c) Opendatalab. All rights reserved.
"""
RAG Pipeline 可观测系统 — 统一追踪、中间结果缓存、可视化。

三合一能力:
1. Pipeline 全流程追踪 (时序/状态/元数据)
2. 中间节点结果缓存 (支持失败后从 checkpoint 恢复)
3. 中间结果可视化 (layout 框线、Dify 增强前后对比、时间线图表)

使用方式:
    tracker = RAGPipelineTracker(output_dir, doc_stem, pdf_bytes, params)
    tracker.start()

    # Stage 1: Azure DI
    if not tracker.has_checkpoint("azure_result"):
        azure_result = await azure_client.analyze_document(pdf_bytes)
        tracker.save_checkpoint("azure_result", azure_result)
    else:
        azure_result = tracker.load_checkpoint("azure_result")

    # ... 各 stage 同理 ...

    tracker.finish()
    tracker.export_report()

缓存路径:
    {output_dir}/.rag_cache/{content_hash}/
    ├── checkpoints/
    │   ├── azure_result.pkl
    │   ├── pages_data.json
    │   ├── dify_results.json
    │   └── middle_json.json
    ├── visualizations/
    │   ├── layout_page_0.png
    │   ├── dify_compare_fig_p0_0.png
    │   └── timeline.png
    └── pipeline_run.json        ← 完整运行报告
"""
import asyncio
import base64
import io
import json
import os
import pickle
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pypdfium2 as pdfium
from loguru import logger
from PIL import Image, ImageDraw, ImageFont

from mineru.utils.hash_utils import bytes_md5
from mineru.utils.pdfium_guard import (
    close_pdfium_document,
    get_pdfium_document_page_count,
    open_pdfium_document,
    pdfium_guard,
)


# ── 配置 ──────────────────────────────────────────────────
CACHE_DIR_NAME = ".rag_cache"
CHECKPOINTS_DIR = "checkpoints"
VIZ_DIR = "visualizations"
RUN_REPORT_NAME = "pipeline_run.json"


# ── 阶段状态枚举 ──────────────────────────────────────────

class StageStatus:
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CACHED = "cached"       # 从缓存恢复, 未重新执行
    SKIPPED = "skipped"


# ── 阶段记录 ──────────────────────────────────────────────

@dataclass
class StageRecord:
    """单个 Pipeline 阶段的运行记录"""
    name: str
    status: str = StageStatus.PENDING
    started_at: float = 0.0
    finished_at: float = 0.0
    duration_s: float = 0.0
    input_summary: dict = field(default_factory=dict)
    output_summary: dict = field(default_factory=dict)
    error_message: str = ""
    error_traceback: str = ""
    from_cache: bool = False


# ── Pipeline 运行报告 ─────────────────────────────────────

@dataclass
class PipelineReport:
    """完整的 Pipeline 运行报告"""
    run_id: str = ""
    doc_stem: str = ""
    content_hash: str = ""
    params: dict = field(default_factory=dict)
    started_at: str = ""
    finished_at: str = ""
    total_duration_s: float = 0.0
    status: str = StageStatus.PENDING
    stages: list[StageRecord] = field(default_factory=list)
    cache_hits: int = 0
    error: str = ""


# ── 主类 ──────────────────────────────────────────────────

class RAGPipelineTracker:
    """
    RAG Pipeline 可观测追踪器。

    职责:
    - 记录每个阶段的起止时间、状态、输入输出概要
    - 在 {cache_dir}/checkpoints/ 下保存中间结果
    - 支持从 checkpoint 恢复, 跳过已完成的阶段
    - 生成可视化产物到 {cache_dir}/visualizations/
    - 导出 pipeline_run.json 运行报告
    """

    def __init__(
        self,
        output_dir: str | Path,
        doc_stem: str,
        pdf_bytes: bytes,
        params: Optional[dict] = None,
    ):
        self.output_dir = Path(output_dir)
        self.doc_stem = doc_stem
        self.pdf_bytes = pdf_bytes
        self.params = params or {}

        # 基于文档内容 + 参数生成缓存 key (前 64KB 取样, 避免大文件 hash 过慢)
        sample = pdf_bytes[:65536] + bytes(str(sorted(self.params.items())), "utf-8")
        self.content_hash = bytes_md5(sample)

        # 缓存目录
        self.cache_root = self.output_dir / CACHE_DIR_NAME / self.content_hash
        self.checkpoint_dir = self.cache_root / CHECKPOINTS_DIR
        self.viz_dir = self.cache_root / VIZ_DIR

        # 运行报告
        self.report = PipelineReport(
            run_id=self._generate_run_id(),
            doc_stem=doc_stem,
            content_hash=self.content_hash,
            params=self.params,
        )
        self._stages: dict[str, StageRecord] = {}
        self._global_start: float = 0.0
        self._pdf_doc = None  # lazy init, 仅用于可视化渲染

    # ── 生命周期 ──────────────────────────────────────────

    def start(self) -> None:
        """Pipeline 开始"""
        self._global_start = time.time()
        self.report.started_at = datetime.now(timezone.utc).isoformat()
        self.report.status = StageStatus.RUNNING
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.viz_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            f"[Tracker] Pipeline started: run_id={self.report.run_id}, "
            f"doc={self.doc_stem}, cache={self.cache_root}"
        )

    def finish(self, error: Optional[Exception] = None) -> None:
        """Pipeline 结束 (成功或失败)"""
        self.report.finished_at = datetime.now(timezone.utc).isoformat()
        self.report.total_duration_s = round(time.time() - self._global_start, 3)
        self.report.stages = list(self._stages.values())

        if error:
            self.report.status = StageStatus.FAILED
            self.report.error = f"{type(error).__name__}: {error}"
        else:
            self.report.status = StageStatus.COMPLETED

        self.report.cache_hits = sum(
            1 for s in self._stages.values() if s.from_cache
        )

        # 清理 PDF 引用
        if self._pdf_doc is not None:
            close_pdfium_document(self._pdf_doc)
            self._pdf_doc = None

        logger.info(
            f"[Tracker] Pipeline finished: status={self.report.status}, "
            f"duration={self.report.total_duration_s}s, "
            f"cache_hits={self.report.cache_hits}/{len(self._stages)}"
        )

    # ── 阶段追踪 ──────────────────────────────────────────

    def start_stage(self, name: str, input_summary: Optional[dict] = None) -> StageRecord:
        """标记一个阶段开始"""
        record = StageRecord(
            name=name,
            status=StageStatus.RUNNING,
            started_at=time.time(),
            input_summary=input_summary or {},
        )
        self._stages[name] = record
        logger.debug(f"[Tracker] Stage '{name}' started")
        return record

    def end_stage(
        self,
        name: str,
        output_summary: Optional[dict] = None,
        error: Optional[Exception] = None,
        from_cache: bool = False,
    ) -> StageRecord:
        """标记一个阶段结束"""
        record = self._stages.get(name)
        if record is None:
            record = StageRecord(name=name)
            self._stages[name] = record

        record.finished_at = time.time()
        record.duration_s = round(record.finished_at - record.started_at, 3)
        record.output_summary = output_summary or {}

        if from_cache:
            record.status = StageStatus.CACHED
            record.from_cache = True
        elif error:
            record.status = StageStatus.FAILED
            record.error_message = str(error)
            record.error_traceback = traceback.format_exc()
        else:
            record.status = StageStatus.COMPLETED

        logger.debug(
            f"[Tracker] Stage '{name}' {record.status} in {record.duration_s}s"
        )
        return record

    # ── 检查点缓存 ──────────────────────────────────────────

    def _checkpoint_path(self, name: str, suffix: str = "pkl") -> Path:
        return self.checkpoint_dir / f"{name}.{suffix}"

    def has_checkpoint(self, name: str) -> bool:
        """检查某个阶段的缓存是否存在"""
        return self._checkpoint_path(name).exists()

    def save_checkpoint(self, name: str, data: Any) -> Path:
        """
        保存中间结果到缓存。

        - 可 pickle 的对象 → .pkl
        - 字符串 → .json (自动尝试 json.loads)
        - dict/list → .json
        """
        # 优先尝试 JSON (可读性好, 便于调试)
        try:
            if isinstance(data, (dict, list)):
                path = self._checkpoint_path(name, "json")
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2, default=str)
                return path

            if isinstance(data, str):
                parsed = json.loads(data)
                path = self._checkpoint_path(name, "json")
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(parsed, f, ensure_ascii=False, indent=2)
                return path
        except (TypeError, ValueError, json.JSONDecodeError):
            pass

        # 回退到 pickle
        path = self._checkpoint_path(name, "pkl")
        with open(path, "wb") as f:
            pickle.dump(data, f)
        return path

    def load_checkpoint(self, name: str) -> Any:
        """
        从缓存恢复中间结果。

        自动检测格式 (JSON vs pickle)。
        """
        json_path = self._checkpoint_path(name, "json")
        if json_path.exists():
            with open(json_path, "r", encoding="utf-8") as f:
                return json.load(f)

        pkl_path = self._checkpoint_path(name, "pkl")
        if pkl_path.exists():
            with open(pkl_path, "rb") as f:
                return pickle.load(f)

        raise FileNotFoundError(f"No checkpoint found for '{name}'")

    def invalidate_checkpoint(self, name: str) -> None:
        """删除某个阶段的缓存 (强制重新执行)"""
        for suffix in ("json", "pkl"):
            path = self._checkpoint_path(name, suffix)
            if path.exists():
                path.unlink()
                logger.debug(f"[Tracker] Invalidated checkpoint: {name}.{suffix}")

    def invalidate_all(self) -> None:
        """删除所有缓存"""
        import shutil
        if self.cache_root.exists():
            shutil.rmtree(self.cache_root)
            self.cache_root.mkdir(parents=True, exist_ok=True)
            logger.info(f"[Tracker] All checkpoints invalidated")

    # ── 可视化 ────────────────────────────────────────────

    def _get_pdf_doc(self):
        """延迟初始化 PDF document (仅用于可视化渲染, 不参与数据处理)"""
        if self._pdf_doc is None:
            self._pdf_doc = open_pdfium_document(pdfium.PdfDocument, self.pdf_bytes)
        return self._pdf_doc

    def render_page_image(self, page_number: int, dpi: int = 150) -> Image.Image:
        """渲染单页为 PIL Image (仅用于可视化)"""
        pdf_doc = self._get_pdf_doc()
        with pdfium_guard():
            page = pdf_doc[page_number]
            bitmap = page.render(scale=dpi / 72.0)
            pil_image = bitmap.to_pil()
        return pil_image

    def draw_layout_boxes(
        self,
        page_number: int,
        blocks: list[dict],
        output_name: Optional[str] = None,
    ) -> Path:
        """
        在页面图片上绘制 Azure DI 检测到的区块框线。

        Args:
            page_number: 页码 (0-based)
            blocks: [{"type": "paragraph"|"table"|"figure", "bbox": [x0,y0,x1,y1], "content": "..."}]
            output_name: 输出文件名 (不含扩展名)

        Returns:
            输出图片路径
        """
        if output_name is None:
            output_name = f"layout_page_{page_number}"

        # 颜色方案
        TYPE_COLORS = {
            "paragraph": (0, 180, 0),      # 绿色
            "table":     (0, 100, 255),    # 蓝色
            "figure":    (255, 50, 50),    # 红色
            "title":     (255, 140, 0),    # 橙色
            "header":    (160, 160, 160),  # 灰色
            "footer":    (160, 160, 160),
        }

        try:
            img = self.render_page_image(page_number)

            # Azure DI 坐标单位是 inch, 需要转换到像素
            # 但重渲染的图片尺寸可能与原图不同, 统一用实际渲染尺寸
            img_w, img_h = img.size
            draw = ImageDraw.Draw(img)

            for block in blocks:
                bbox = block.get("bbox")
                if not bbox or len(bbox) != 4:
                    continue

                block_type = block.get("type", "paragraph")
                color = TYPE_COLORS.get(block_type, (128, 128, 128))

                # bbox 坐标是 inch 单位, 转为像素 (基于 72dpi 基础坐标系)
                # Azure DI 返回的坐标基于原始页面尺寸
                scale_x = img_w / (block.get("_page_width", img_w) or img_w)
                scale_y = img_h / (block.get("_page_height", img_h) or img_h)

                x0, y0, x1, y1 = bbox
                px0, py0 = int(x0 * scale_x), int(y0 * scale_y)
                px1, py1 = int(x1 * scale_x), int(y1 * scale_y)

                # 绘制矩形框
                draw.rectangle([px0, py0, px1, py1], outline=color, width=2)

                # 绘制标签
                label = block_type[:12]
                content_preview = (block.get("content") or "")[:30]
                label_text = f"{label}"
                if content_preview:
                    label_text += f": {content_preview}"

                # 标签背景
                text_bbox = draw.textbbox((px0 + 3, py0 - 14), label_text)
                draw.rectangle(text_bbox, fill=color)
                draw.text((px0 + 3, py0 - 14), label_text, fill=(255, 255, 255))

            # 保存
            output_path = self.viz_dir / f"{output_name}.png"
            img.save(str(output_path), "PNG")
            logger.debug(f"[Viz] Layout boxes → {output_path}")
            return output_path

        except Exception as e:
            logger.warning(f"[Viz] Failed to render layout boxes for page {page_number}: {e}")
            return self.viz_dir / f"{output_name}_failed.txt"

    def draw_dify_comparison(
        self,
        original_text: str,
        enhanced_text: str,
        item_type: str,    # "image" or "table"
        item_key: str,
    ) -> Path:
        """
        生成 Dify 增强前后对比的文本可视化。

        Returns:
            对比文件路径 (.txt 格式)
        """
        output_path = self.viz_dir / f"dify_compare_{item_type}_{item_key}.txt"

        lines = [
            "=" * 80,
            f"Dify Enhancement Comparison — {item_type}: {item_key}",
            "=" * 80,
            "",
            "-" * 40,
            "ORIGINAL",
            "-" * 40,
            original_text[:3000] if original_text else "(empty)",
            "",
            "-" * 40,
            "ENHANCED (Dify)",
            "-" * 40,
            enhanced_text[:3000] if enhanced_text else "(empty)",
            "",
        ]

        # 简单 diff 标记
        if original_text and enhanced_text and original_text != enhanced_text:
            orig_words = set(original_text.split())
            enhanced_words = set(enhanced_text.split())
            added = enhanced_words - orig_words
            removed = orig_words - enhanced_words
            if added:
                lines.append(f"[+] Added terms: {', '.join(sorted(added)[:20])}")
            if removed:
                lines.append(f"[-] Removed terms: {', '.join(sorted(removed)[:20])}")

        with open(output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        return output_path

    def draw_pipeline_timeline(self) -> Path:
        """
        生成 Pipeline 阶段时序文本图。

        Returns:
            时间线图路径 (.txt 格式)
        """
        output_path = self.viz_dir / "timeline.txt"

        total = self.report.total_duration_s or 1
        lines = [
            "Pipeline Timeline",
            "=" * 60,
            f"Run: {self.report.run_id}",
            f"Doc:  {self.doc_stem}",
            f"Status: {self.report.status}",
            f"Total: {total:.1f}s",
            "",
            f" {'Stage':<25s} {'Time':>8s} {'%':>6s} {'Status':>10s}  Bar",
            f" {'-'*25} {'-'*8} {'-'*6} {'-'*10}  {'-'*20}",
        ]

        for stage in self._stages.values():
            pct = min(stage.duration_s / total * 100, 100) if total > 0 else 0
            bar_len = max(int(pct * 20 / 100), 1)
            bar = "█" * bar_len + "░" * (20 - bar_len)

            status_mark = {
                StageStatus.COMPLETED: "✅",
                StageStatus.CACHED: "💾",
                StageStatus.FAILED: "❌",
                StageStatus.SKIPPED: "⏭️",
                StageStatus.RUNNING: "🔄",
            }.get(stage.status, "❓")

            lines.append(
                f" {stage.name:<25s} {stage.duration_s:>7.1f}s {pct:>5.1f}% "
                f"{status_mark} {stage.status:<8s} {bar}"
            )

        with open(output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        return output_path

    # ── 报告导出 ──────────────────────────────────────────

    def export_report(self) -> Path:
        """
        导出完整的 pipeline_run.json 运行报告。

        Returns:
            报告文件路径
        """
        report_path = self.cache_root / RUN_REPORT_NAME

        report_dict = {
            "run_id": self.report.run_id,
            "doc_stem": self.report.doc_stem,
            "content_hash": self.report.content_hash,
            "params": self.report.params,
            "started_at": self.report.started_at,
            "finished_at": self.report.finished_at,
            "total_duration_s": self.report.total_duration_s,
            "status": self.report.status,
            "cache_hits": sum(1 for s in self._stages.values() if s.from_cache),
            "cache_dir": str(self.cache_root),
            "stages": {},
            "error": self.report.error,
        }

        for name, stage in self._stages.items():
            report_dict["stages"][name] = {
                "status": stage.status,
                "duration_s": stage.duration_s,
                "from_cache": stage.from_cache,
                "input_summary": stage.input_summary,
                "output_summary": stage.output_summary,
                "error": stage.error_message[:500] if stage.error_message else None,
            }

        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report_dict, f, ensure_ascii=False, indent=2)

        logger.info(f"[Tracker] Report exported → {report_path}")
        return report_path

    def export_stage_summary(self) -> str:
        """生成阶段摘要字符串 (日志输出用)"""
        parts = []
        for name, stage in self._stages.items():
            cache_mark = " [CACHE]" if stage.from_cache else ""
            status_mark = "✓" if stage.status in (StageStatus.COMPLETED, StageStatus.CACHED) else "✗"
            parts.append(
                f"{status_mark} {name}: {stage.duration_s:.1f}s{cache_mark}"
                f" → {stage.output_summary.get('summary', '')}"
            )
        return " | ".join(parts) if parts else "(no stages)"

    # ── 内部方法 ──────────────────────────────────────────

    @staticmethod
    def _generate_run_id() -> str:
        """生成唯一的运行 ID"""
        import uuid
        return uuid.uuid4().hex[:12]


# ── 便捷装饰器: 包装阶段追踪 ──────────────────────────────

def track_stage(
    tracker: RAGPipelineTracker,
    stage_name: str,
    checkpoint_name: Optional[str] = None,
    input_summary: Optional[dict] = None,
):
    """
    装饰器工厂: 用 tracker 包装一个异步函数, 自动记录阶段 + 缓存管理。

    使用示例:
        @track_stage(tracker, "azure_di", checkpoint_name="azure_result")
        async def run_azure_di(pdf_bytes):
            return await azure_client.analyze_document(pdf_bytes)

    如果 checkpoint 存在, 函数体不会执行, 直接从缓存返回。
    """
    def decorator(func):
        async def wrapper(*args, **kwargs):
            ckpt = checkpoint_name or stage_name

            # 检查缓存
            if tracker.has_checkpoint(ckpt):
                logger.info(f"[Tracker] Stage '{stage_name}' → cache hit, skipping execution")
                tracker.start_stage(stage_name, input_summary)
                result = tracker.load_checkpoint(ckpt)
                tracker.end_stage(
                    stage_name,
                    output_summary={"summary": "from cache", "cache_key": ckpt},
                    from_cache=True,
                )
                return result

            # 执行
            tracker.start_stage(stage_name, input_summary)
            try:
                result = await func(*args, **kwargs)
                # 保存缓存
                try:
                    tracker.save_checkpoint(ckpt, result)
                except Exception as e:
                    logger.warning(f"[Tracker] Failed to save checkpoint '{ckpt}': {e}")
                tracker.end_stage(
                    stage_name,
                    output_summary={"summary": "completed"},
                )
                return result
            except Exception as e:
                tracker.end_stage(stage_name, error=e)
                raise

        return wrapper
    return decorator

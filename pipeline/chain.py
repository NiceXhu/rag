# Copyright (c) Opendatalab. All rights reserved.
"""
Pipeline 链式编排器。

PipelineChain 按顺序执行一组 PipelineStage,
支持通过配置 JSON 定义处理流程, 实现完全可配置的文档处理链。
"""
import json
import time
from pathlib import Path
from typing import Optional

from loguru import logger

from mineru.backend.rag.pipeline.context import PipelineContext
from mineru.backend.rag.pipeline.stage import PipelineStage, StageConfig
from mineru.backend.rag.pipeline.registry import StageRegistry


class PipelineChain:
    """
    可配置的 Pipeline 链。

    使用方式:
        # 方式1: 代码构建
        chain = PipelineChain([
            PDFLoadStage(),
            AzureDIAnalyzeStage(),
            DifyEnhanceStage(),
        ])
        ctx = await chain.run(PipelineContext(pdf_bytes=..., ...))

        # 方式2: 配置文件构建
        chain = PipelineChain.from_config("pipelines/rag_default.json")
        ctx = await chain.run(ctx)

        # 方式3: 名称列表构建
        chain = PipelineChain.from_names(["pdf_load", "azure_di", "page_group",
                                           "borderless_table", "image_filter",
                                           "table_merge", "dify_enhance",
                                           "hyperlink_map", "build_middle_json"])
    """

    def __init__(self, stages: Optional[list[PipelineStage]] = None):
        self.stages: list[PipelineStage] = stages or []
        self._registry = StageRegistry()

    def add_stage(self, stage: PipelineStage) -> "PipelineChain":
        """追加一个阶段到链尾"""
        self.stages.append(stage)
        return self

    def insert_before(self, target_name: str, stage: PipelineStage) -> "PipelineChain":
        """在指定阶段之前插入"""
        for i, s in enumerate(self.stages):
            if s.name == target_name:
                self.stages.insert(i, stage)
                return self
        raise ValueError(f"Stage '{target_name}' not found in chain")

    def insert_after(self, target_name: str, stage: PipelineStage) -> "PipelineChain":
        """在指定阶段之后插入"""
        for i, s in enumerate(self.stages):
            if s.name == target_name:
                self.stages.insert(i + 1, stage)
                return self
        raise ValueError(f"Stage '{target_name}' not found in chain")

    def remove(self, name: str) -> "PipelineChain":
        """移除指定阶段"""
        self.stages = [s for s in self.stages if s.name != name]
        return self

    def replace(self, name: str, new_stage: PipelineStage) -> "PipelineChain":
        """替换指定阶段"""
        for i, s in enumerate(self.stages):
            if s.name == name:
                self.stages[i] = new_stage
                return self
        raise ValueError(f"Stage '{name}' not found in chain")

    def enable(self, name: str) -> "PipelineChain":
        """启用指定阶段"""
        for s in self.stages:
            if s.name == name:
                s.config.enabled = True
        return self

    def disable(self, name: str) -> "PipelineChain":
        """禁用指定阶段"""
        for s in self.stages:
            if s.name == name:
                s.config.enabled = False
        return self

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        """
        按顺序执行为所有启用的阶段。

        每个阶段:
        - 如果 disabled → 跳过
        - 如果 checkpoint 存在 → 从缓存恢复
        - 否则 → 执行 execute()
        - 如果 required=True 且失败 → 中止整个 Pipeline
        """
        chain_start = time.time()
        ctx.init_tracker()

        enabled = [s for s in self.stages if s.config.enabled]
        logger.info(
            f"PipelineChain: {len(enabled)}/{len(self.stages)} stages enabled → "
            + " → ".join(s.name for s in enabled)
        )

        for stage in self.stages:
            try:
                ctx = await stage.run(ctx)
            except Exception as e:
                logger.exception(f"PipelineChain 中止于 {stage.name}: {e}")
                if ctx.tracker:
                    ctx.tracker.finish(error=e)
                    ctx.tracker.export_report()
                raise

        chain_time = round(time.time() - chain_start, 2)
        logger.info(f"PipelineChain 完成: {chain_time}s")

        if ctx.tracker:
            ctx.tracker.finish()
            ctx.tracker.export_report()

        return ctx

    def describe(self) -> str:
        """生成链的描述文本"""
        lines = ["PipelineChain:"]
        for i, s in enumerate(self.stages):
            status = "✓" if s.config.enabled else "✗"
            ckpt = "💾" if s.config.checkpoint else "  "
            lines.append(f"  {i+1}. [{status}] {ckpt} {s.name}")
        return "\n".join(lines)

    # ── 工厂方法 ──

    @classmethod
    def from_names(
        cls,
        names: list[str],
        stage_configs: Optional[dict[str, StageConfig]] = None,
    ) -> "PipelineChain":
        """
        从名称列表构建链。

        Args:
            names: ["pdf_load", "azure_di", "dify_enhance", ...]
            stage_configs: {name: StageConfig} 可选配置覆盖
        """
        registry = StageRegistry()
        configs = stage_configs or {}
        stages = []

        for name in names:
            stage_cls = registry.get(name)
            if stage_cls is None:
                raise ValueError(
                    f"Unknown stage: '{name}'. "
                    f"Available: {registry.list_names()}"
                )
            cfg = configs.get(name, StageConfig())
            stages.append(stage_cls(config=cfg))

        return cls(stages)

    @classmethod
    def from_config(cls, config_path: str | Path) -> "PipelineChain":
        """
        从 JSON 配置文件构建链。

        配置格式:
        {
          "name": "rag_default",
          "stages": [
            {"name": "pdf_load", "enabled": true, "checkpoint": false},
            {"name": "azure_di", "enabled": true, "checkpoint": true},
            {"name": "page_group", "enabled": true, "checkpoint": true, "params": {...}},
            ...
          ]
        }
        """
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)

        registry = StageRegistry()
        stages = []

        for stage_spec in config.get("stages", []):
            name = stage_spec["name"]
            stage_cls = registry.get(name)
            if stage_cls is None:
                raise ValueError(f"Unknown stage: '{name}'")

            stage_cfg = StageConfig(
                enabled=stage_spec.get("enabled", True),
                checkpoint=stage_spec.get("checkpoint", True),
                required=stage_spec.get("required", True),
                timeout_s=stage_spec.get("timeout_s", 0),
                params=stage_spec.get("params", {}),
            )
            stages.append(stage_cls(config=stage_cfg))

        chain = cls(stages)
        logger.info(f"PipelineChain loaded from {config_path}: {chain.describe()}")
        return chain


# ── 预置链配置 ────────────────────────────────────────────

def default_rag_chain() -> PipelineChain:
    """默认 RAG 处理链"""
    return PipelineChain.from_names([
        "pdf_load",
        "azure_di",
        "page_group",
        "borderless_table",
        "image_filter",
        "table_merge",
        "dify_enhance",
        "hyperlink_map",
        "build_middle_json",
        "model_output",
    ])


def minimal_rag_chain() -> PipelineChain:
    """最小 RAG 处理链 (仅 Azure DI, 跳过 Dify)"""
    return PipelineChain.from_names([
        "pdf_load",
        "azure_di",
        "page_group",
        "table_merge",
        "build_middle_json",
        "model_output",
    ])


def office_rag_chain() -> PipelineChain:
    """Office 文档处理链 (关闭图片过滤, 因为 Office 图片通常是内容)"""
    chain = default_rag_chain()
    chain.disable("image_filter")
    return chain


def excel_chain() -> PipelineChain:
    """Excel 处理链 — 单阶段, 直接读取 Sheet + Dify 优化"""
    return PipelineChain.from_names([
        "excel_process",
    ])

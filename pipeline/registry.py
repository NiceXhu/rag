# Copyright (c) Opendatalab. All rights reserved.
"""
阶段注册表 — 管理所有可用的 Pipeline 阶段。

支持:
- 内置阶段注册
- 用户自定义阶段 (通过 Python API 注册)
- 通过名称查找阶段类
"""
from typing import Optional, Type

from loguru import logger

from mineru.backend.rag.pipeline.stage import PipelineStage


class StageRegistry:
    """
    全局阶段注册表 (单例模式)。

    内置阶段在首次导入时自动注册。
    用户可以注册自定义阶段:
        StageRegistry().register("my_filter", MyCustomFilterStage)
    """

    _instance: Optional["StageRegistry"] = None
    _stages: dict[str, Type[PipelineStage]] = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._stages = {}
        return cls._instance

    def register(self, name: str, stage_cls: Type[PipelineStage]) -> None:
        """注册一个阶段类"""
        if name in self._stages:
            existing = self._stages[name].__name__
            logger.warning(f"阶段 '{name}' 已注册 ({existing}), 将被覆盖")
        self._stages[name] = stage_cls

    def get(self, name: str) -> Optional[Type[PipelineStage]]:
        """按名称获取阶段类"""
        return self._stages.get(name)

    def list_names(self) -> list[str]:
        """列出所有注册的阶段名称"""
        return sorted(self._stages.keys())

    def list_all(self) -> dict[str, Type[PipelineStage]]:
        """列出所有注册的阶段"""
        return dict(self._stages)

    def unregister(self, name: str) -> None:
        """取消注册"""
        self._stages.pop(name, None)


# ── 自动注册内置阶段 ──

def _register_builtin_stages():
    """注册所有内置阶段 (延迟导入, 避免循环依赖)"""
    from mineru.backend.rag.pipeline.stages.pdf_load import PDFLoadStage
    from mineru.backend.rag.pipeline.stages.azure_di import AzureDIAnalyzeStage
    from mineru.backend.rag.pipeline.stages.page_group import PageGroupStage
    from mineru.backend.rag.pipeline.stages.borderless_table import BorderlessTableStage
    from mineru.backend.rag.pipeline.stages.image_filter import ImageFilterStage
    from mineru.backend.rag.pipeline.stages.table_merge import TableMergeStage
    from mineru.backend.rag.pipeline.stages.dify_enhance import DifyEnhanceStage
    from mineru.backend.rag.pipeline.stages.hyperlink_map import HyperlinkMapStage
    from mineru.backend.rag.pipeline.stages.build_middle_json import BuildMiddleJsonStage
    from mineru.backend.rag.pipeline.stages.model_output import ModelOutputStage
    from mineru.backend.rag.pipeline.stages.excel_process import ExcelProcessStage

    registry = StageRegistry()
    registry.register("pdf_load", PDFLoadStage)
    registry.register("azure_di", AzureDIAnalyzeStage)
    registry.register("page_group", PageGroupStage)
    registry.register("borderless_table", BorderlessTableStage)
    registry.register("image_filter", ImageFilterStage)
    registry.register("table_merge", TableMergeStage)
    registry.register("dify_enhance", DifyEnhanceStage)
    registry.register("hyperlink_map", HyperlinkMapStage)
    registry.register("build_middle_json", BuildMiddleJsonStage)
    registry.register("model_output", ModelOutputStage)
    registry.register("excel_process", ExcelProcessStage)


# 模块加载时自动注册
_register_builtin_stages()

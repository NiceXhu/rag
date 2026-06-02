# Copyright (c) Opendatalab. All rights reserved.
"""
Pipeline 共享上下文。

PipelineContext 是流经所有处理阶段的共享状态对象。
每个阶段从中读取上游输出, 写入本阶段的产出,
Tracker 记录全流程的运行状态。
"""
from dataclasses import dataclass, field
from typing import Any, Optional

from mineru.backend.rag.observability import RAGPipelineTracker


@dataclass
class PipelineContext:
    """
    Pipeline 共享上下文。

    生命周期:
    1. 调用方创建 ctx = PipelineContext(pdf_bytes=..., output_dir=..., doc_stem=...)
    2. PipelineChain 将 ctx 依次传入各阶段
    3. 各阶段通过 ctx.<field> 读写中间结果
    4. Pipeline 结束后, ctx.middle_json 和 ctx.model_output 包含最终结果
    """

    # ── 输入 (创建时设置, 只读) ──
    pdf_bytes: bytes
    output_dir: str = "."
    doc_stem: str = "document"
    params: dict = field(default_factory=dict)

    # ── 中间结果 (各阶段写入) ──
    effective_pdf_bytes: Optional[bytes] = None    # page range 裁剪后的 PDF
    azure_result: Optional[dict] = None            # Azure DI 原始返回
    pages_data: Optional[list[dict]] = None        # 按页分组的数据
    dify_image_results: list = field(default_factory=list)   # Dify 图片增强结果
    dify_table_results: list = field(default_factory=list)   # Dify 表格优化结果
    middle_json: Optional[dict] = None             # 最终 middle_json
    model_output: Optional[dict] = None            # 最终 model_output

    # ── 元数据 ──
    tracker: Optional[RAGPipelineTracker] = None
    stage_results: dict[str, Any] = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)   # 自由扩展字段

    # ── 错误收集 ──
    errors: list[dict] = field(default_factory=list)

    @property
    def is_healthy(self) -> bool:
        """Pipeline 是否没有致命错误"""
        return len([e for e in self.errors if e.get("fatal", False)]) == 0

    def add_error(self, stage: str, message: str, fatal: bool = False):
        """记录一个阶段错误"""
        self.errors.append({"stage": stage, "message": message, "fatal": fatal})

    def init_tracker(self) -> RAGPipelineTracker:
        """初始化或返回已有的 tracker"""
        if self.tracker is None:
            self.tracker = RAGPipelineTracker(
                output_dir=self.output_dir,
                doc_stem=self.doc_stem,
                pdf_bytes=self.pdf_bytes,
                params=self.params,
            )
            self.tracker.start()
        return self.tracker

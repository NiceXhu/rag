# Copyright (c) Opendatalab. All rights reserved.
"""
PDF 超链接回插阶段。
"""
from mineru.backend.rag.pipeline.stage import PipelineStage, StageConfig
from mineru.backend.rag.pipeline.context import PipelineContext
from mineru.backend.rag.hyperlink_mapper import apply_hyperlinks_to_middle_json


class HyperlinkMapStage(PipelineStage):
    name = "hyperlink_map"

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        if not ctx.middle_json:
            raise ValueError("middle_json is empty — 确保 build_middle_json 阶段先执行")
        apply_hyperlinks_to_middle_json(ctx.middle_json, ctx.pdf_bytes)
        return ctx

    def _build_output_summary(self, ctx: PipelineContext) -> dict:
        return {"status": "completed"}

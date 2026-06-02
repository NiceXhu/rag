# Copyright (c) Opendatalab. All rights reserved.
"""
图片相关性过滤阶段。
"""
from mineru.backend.rag.pipeline.stage import PipelineStage, StageConfig
from mineru.backend.rag.pipeline.context import PipelineContext
from mineru.backend.rag.image_relevance import apply_image_filter


class ImageFilterStage(PipelineStage):
    name = "image_filter"

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        if not ctx.pages_data:
            raise ValueError("pages_data is empty")
        result = apply_image_filter(ctx.pages_data)
        ctx.metadata["image_filter"] = {
            "total": result.total,
            "kept": result.kept,
            "skipped": result.skipped,
        }
        return ctx

    def _build_output_summary(self, ctx: PipelineContext) -> dict:
        info = ctx.metadata.get("image_filter", {})
        return {"kept": info.get("kept", 0), "skipped": info.get("skipped", 0)}

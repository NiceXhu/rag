# Copyright (c) Opendatalab. All rights reserved.
"""
PDF 加载阶段 — 处理页范围裁剪。
"""
from mineru.backend.rag.pipeline.stage import PipelineStage, StageConfig
from mineru.backend.rag.pipeline.context import PipelineContext
from mineru.utils.pdfium_guard import rewrite_pdf_bytes_with_pdfium


class PDFLoadStage(PipelineStage):
    name = "pdf_load"

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        start = ctx.params.get("start_page_id", 0)
        end = ctx.params.get("end_page_id", None)

        if start > 0 or end is not None:
            ctx.effective_pdf_bytes = rewrite_pdf_bytes_with_pdfium(
                ctx.pdf_bytes,
                start_page_id=start,
                end_page_id=end,
            )
        else:
            ctx.effective_pdf_bytes = ctx.pdf_bytes

        return ctx

    def _build_output_summary(self, ctx: PipelineContext) -> dict:
        return {"pdf_bytes": len(ctx.effective_pdf_bytes or b"")}

# Copyright (c) Opendatalab. All rights reserved.
"""
Excel 处理阶段 — 读取所有 Sheet, Dify 优化, 输出 Markdown。
"""
from mineru.backend.rag.pipeline.stage import PipelineStage, StageConfig
from mineru.backend.rag.pipeline.context import PipelineContext
from mineru.backend.rag.excel_processor import process_excel, build_excel_markdown
from mineru.backend.rag.dify_client import DifyWorkflowClient


class ExcelProcessStage(PipelineStage):
    name = "excel_process"

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        dify_client = DifyWorkflowClient()

        result = await process_excel(
            file_bytes=ctx.pdf_bytes,   # ctx.pdf_bytes 存储的是 Excel 文件字节流
            file_name=f"{ctx.doc_stem}.xlsx",
            dify_client=dify_client if dify_client.is_configured else None,
            max_dify_rows=self.config.params.get("max_dify_rows", 1000),
            skip_dify_threshold=self.config.params.get("skip_dify_threshold", 5000),
        )

        # 构建 Markdown 并存入 ctx.middle_json 兼容结构
        markdown = build_excel_markdown(result)

        # 存为单页 middle_json (兼容输出框架)
        ctx.middle_json = {
            "pdf_info": [{
                "preproc_blocks": [{
                    "type": "text",
                    "lines": [{"spans": [{"type": "text", "content": markdown}]}],
                }],
                "page_idx": 0,
                "page_size": [0, 0],
                "discarded_blocks": [],
            }],
            "_backend": "rag_excel",
        }

        ctx.metadata["excel_result"] = result
        return ctx

    def _build_output_summary(self, ctx: PipelineContext) -> dict:
        r = ctx.metadata.get("excel_result")
        if r is None:
            return {}
        return {
            "sheets": len(r.sheets),
            "total_rows": r.total_rows,
            "dify_calls": r.dify_calls,
            "time_s": r.processing_time_s,
        }

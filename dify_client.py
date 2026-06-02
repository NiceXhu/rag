# Copyright (c) Opendatalab. All rights reserved.
"""
Dify Workflow 客户端封装。

通过 HTTP API 调用 Dify Workflow 对图片进行描述生成和表格优化处理。
使用共享 httpx.AsyncClient 连接池, 避免每次调用重建 TCP+TLS 连接。
"""
import asyncio
import base64
import os
from dataclasses import dataclass
from typing import Optional

import httpx
from loguru import logger


# ── 配置常量 ─────────────────────────────────────────────────
DIFY_API_BASE_URL_ENV = "DIFY_API_BASE_URL"
DIFY_IMAGE_WORKFLOW_API_KEY_ENV = "DIFY_IMAGE_WORKFLOW_API_KEY"
DIFY_TABLE_WORKFLOW_API_KEY_ENV = "DIFY_TABLE_WORKFLOW_API_KEY"
DIFY_IMAGE_WORKFLOW_ENDPOINT = "/v1/workflows/run"
DIFY_TABLE_WORKFLOW_ENDPOINT = "/v1/workflows/run"

DIFY_MAX_RETRIES = 3
DIFY_RETRY_DELAY = 1.5
DIFY_TIMEOUT = 120
DIFY_MAX_CONCURRENT = 16
# 连接池: 保持 20 个 keep-alive 连接, 单个 host 最多 50 连接
DIFY_MAX_KEEPALIVE = 20
DIFY_MAX_CONNECTIONS = 50


@dataclass
class DifyImageResult:
    """图片分析结果"""
    image_key: str
    page_number: int
    bbox: Optional[list[int]] = None
    description: str = ""
    category: str = ""
    confidence: float = 0.0


@dataclass
class DifyTableResult:
    """表格优化结果"""
    table_index: int
    page_number: int
    bbox: Optional[list[int]] = None
    optimized_html: str = ""
    optimized_markdown: str = ""
    caption: str = ""
    confidence: float = 0.0


class DifyWorkflowClient:
    """Dify Workflow HTTP 客户端 (单例 + 连接池复用)"""

    _instance: Optional["DifyWorkflowClient"] = None
    _lock = __import__('threading').RLock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(
        self,
        base_url: Optional[str] = None,
        image_api_key: Optional[str] = None,
        table_api_key: Optional[str] = None,
    ):
        with self._lock:
            if hasattr(self, '_initialized') and self._initialized:
                return
            self._base_url = (base_url or os.getenv(DIFY_API_BASE_URL_ENV, "")).rstrip("/")
            self._image_api_key = image_api_key or os.getenv(DIFY_IMAGE_WORKFLOW_API_KEY_ENV, "")
            self._table_api_key = table_api_key or os.getenv(DIFY_TABLE_WORKFLOW_API_KEY_ENV, "")
            self._semaphore = asyncio.Semaphore(DIFY_MAX_CONCURRENT)
            # ★ 共享连接池 — 避免每次调用重建 TCP+TLS
            self._http_client: Optional[httpx.AsyncClient] = None
            self._initialized = True

    @property
    def is_configured(self) -> bool:
        return bool(self._base_url and (self._image_api_key or self._table_api_key))

    @property
    def image_configured(self) -> bool:
        return bool(self._base_url and self._image_api_key)

    @property
    def table_configured(self) -> bool:
        return bool(self._base_url and self._table_api_key)

    def _get_http_client(self) -> httpx.AsyncClient:
        """获取或懒惰创建共享的 httpx 客户端 (连接池复用)"""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                timeout=DIFY_TIMEOUT,
                limits=httpx.Limits(
                    max_keepalive_connections=DIFY_MAX_KEEPALIVE,
                    max_connections=DIFY_MAX_CONNECTIONS,
                ),
                http2=False,
                follow_redirects=True,
            )
        return self._http_client

    async def close(self) -> None:
        """关闭 HTTP 客户端, 释放连接"""
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()
            self._http_client = None

    async def _call_workflow(
        self, api_key: str, inputs: dict,
        endpoint: str = DIFY_IMAGE_WORKFLOW_ENDPOINT,
    ) -> dict:
        """调用 Dify Workflow API — 复用共享连接池"""
        if not self._base_url or not api_key:
            raise RuntimeError(
                "Dify workflow not configured. "
                "Set DIFY_API_BASE_URL and workflow API key env vars."
            )

        url = f"{self._base_url}{endpoint}"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "inputs": inputs,
            "response_mode": "blocking",
            "user": "mineru-rag-pipeline",
        }
        client = self._get_http_client()

        for attempt in range(DIFY_MAX_RETRIES):
            try:
                async with self._semaphore:
                    response = await client.post(url, headers=headers, json=payload)
                    response.raise_for_status()
                    data = response.json()
                    return data.get("data", {}).get("outputs", data)

            except (httpx.TimeoutException, httpx.HTTPStatusError) as e:
                logger.warning(
                    f"Dify call failed (attempt {attempt + 1}/{DIFY_MAX_RETRIES}): {e}"
                )
                if attempt < DIFY_MAX_RETRIES - 1:
                    await asyncio.sleep(DIFY_RETRY_DELAY * (attempt + 1))
                else:
                    raise RuntimeError(
                        f"Dify workflow failed after {DIFY_MAX_RETRIES} attempts: {e}"
                    ) from e

    async def analyze_image(
        self, image_base64: str, image_key: str, page_number: int,
        bbox: Optional[list[int]] = None, context_text: str = "",
    ) -> DifyImageResult:
        if not self._image_api_key:
            return DifyImageResult(
                image_key=image_key, page_number=page_number, bbox=bbox,
            )

        result = await self._call_workflow(
            api_key=self._image_api_key,
            inputs={
                "image_base64": image_base64,
                "image_key": image_key,
                "page_number": str(page_number),
                "context_text": context_text[:2000],
            },
            endpoint=DIFY_IMAGE_WORKFLOW_ENDPOINT,
        )

        return DifyImageResult(
            image_key=image_key, page_number=page_number, bbox=bbox,
            description=result.get("description", result.get("text", "")),
            category=result.get("category", "image"),
            confidence=float(result.get("confidence", 0)),
        )

    async def optimize_table(
        self, table_html: str, table_index: int, page_number: int,
        bbox: Optional[list[int]] = None, caption: str = "", context_text: str = "",
    ) -> DifyTableResult:
        if not self._table_api_key:
            return DifyTableResult(
                table_index=table_index, page_number=page_number,
                bbox=bbox, optimized_html=table_html, caption=caption,
            )

        result = await self._call_workflow(
            api_key=self._table_api_key,
            inputs={
                "table_html": table_html,
                "table_index": str(table_index),
                "page_number": str(page_number),
                "caption": caption,
                "context_text": context_text[:2000],
            },
            endpoint=DIFY_TABLE_WORKFLOW_ENDPOINT,
        )

        return DifyTableResult(
            table_index=table_index, page_number=page_number, bbox=bbox,
            optimized_html=result.get("optimized_html", table_html),
            optimized_markdown=result.get("optimized_markdown", ""),
            caption=result.get("caption", caption),
            confidence=float(result.get("confidence", 0)),
        )


def encode_image_to_base64(image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
    b64_str = base64.b64encode(image_bytes).decode("utf-8")
    return f"data:{mime_type};base64,{b64_str}"

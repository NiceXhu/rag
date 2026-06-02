# MinerU RAG 数据处理 Pipeline 文档

> 基于 Azure Document Intelligence + Dify Workflow 的智能文档解析增强流水线
>
> 支持 PDF · 图片 · Excel · DOCX · PPTX, 自动识别文件类型并路由到对应处理器

---

## 目录

1. [架构概览](#1-架构概览)
2. [快速开始](#2-快速开始)
3. [文件类型自动路由](#3-文件类型自动路由)
4. [PDF 处理流程详解](#4-pdf-处理流程详解)
5. [Excel 处理](#5-excel-处理)
6. [配置体系](#6-配置体系)
7. [Pipeline Chain API](#7-pipeline-chain-api)
8. [扩展指南](#8-扩展指南)
9. [可观测系统](#9-可观测系统)
10. [可视化看板](#10-可视化看板)
11. [环境变量参考](#11-环境变量参考)
12. [常见问题](#12-常见问题)

---

## 1. 架构概览

### 1.1 整体架构

```
  输入目录 (混合格式: PDF + Excel + DOCX + PPTX + 图片)
                            │
                            ▼
                   ┌──────────────────┐
                   │  collect_files   │
                   │  _by_type()      │  ← 按后缀自动分组
                   └──────┬───────────┘
                          │
          ┌───────────────┼───────────────┬───────────────┐
          │               │               │               │
     PDF/图片           Excel           DOCX            PPTX
          │               │               │               │
          ▼               ▼               ▼               ▼
   ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌────────────┐
   │ RAG Chain  │  │Excel Chain │  │Office Chain│  │Office Chain│
   │ (10阶段)   │  │ (1阶段)    │  │ (原生解析)  │  │ (原生解析)  │
   │            │  │            │  │            │  │            │
   │ PDF Load   │  │ Excel      │  │ python-    │  │ pypptx     │
   │ Azure DI   │  │ Process    │  │ docx       │  │ 原生       │
   │ Page Group │  │  ├ 所有Sheet│  │ 原生       │  │            │
   │ Borderless │  │  ├ HTML    │  │            │  │            │
   │ Img Filter │  │  ├ Dify    │  │            │  │            │
   │ Table Merge│  │  └ MD输出  │  │            │  │            │
   │ Dify Enh.  │  │            │  │            │  │            │
   │ Hyperlink  │  └────────────┘  └────────────┘  └────────────┘
   │ Build MD   │        │               │               │
   │ Output     │        │               │               │
   └────────────┘        │               │               │
          │               │               │               │
          └───────────────┴───────────────┴───────────────┘
                          │
                          ▼
                 ┌──────────────────┐
                 │  统一输出目录      │
                 │  *.md / *.json   │
                 └──────────────────┘
```

### 1.2 核心设计原则

| 原则 | 实现 |
|------|------|
| **解耦** | 每个阶段通过 `PipelineContext` 共享状态, 无直接依赖 |
| **模块化** | 每阶段是独立的 `PipelineStage` 子类, 可单独测试 |
| **可扩展** | 通过 `StageRegistry` 注册自定义阶段, 通过 `PipelineChain` 灵活编排 |
| **可缓存** | 每阶段自动 checkpoint, 失败后从断点恢复 |
| **可观测** | `RAGPipelineTracker` 追踪每阶段耗时/状态/缓存命中 |

### 1.3 三层并发

```
Level 1: Azure DI (服务端)
  单次 PDF 调用 → Azure 内部自动并行各页
  客户端不需要分片

Level 2: Dify Enhancement (客户端并发)
  图片 + 表格任务混合竞争 asyncio.Semaphore(8)
  httpx 连接池复用 (TCP+TLS 握手一次)

Level 3: 可视化生成 (后台异步)
  asyncio.create_task 不阻塞主流程
```

---

## 2. 快速开始

### 2.1 安装依赖

```bash
pip install azure-ai-documentintelligence httpx
```

### 2.2 配置环境变量

```bash
# Azure Document Intelligence
export AZURE_DOC_INTELLIGENCE_ENDPOINT="https://<resource>.cognitiveservices.azure.com/"
export AZURE_DOC_INTELLIGENCE_KEY="<your-key>"

# Dify Workflow (可选 — 不配置则跳过增强)
export DIFY_API_BASE_URL="https://<dify-instance>/api"
export DIFY_IMAGE_WORKFLOW_API_KEY="<image-workflow-key>"
export DIFY_TABLE_WORKFLOW_API_KEY="<table-workflow-key>"
```

### 2.3 基础用法

```python
import asyncio
from mineru.backend.rag.rag_analyze import parse_document

# 一站式解析
result = asyncio.run(parse_document(
    file_path="/path/to/document.pdf",
    output_dir="./output",
))

print(result["output_dir"])  # ./output/document/rag_auto/
```

### 2.4 CLI 用法

```bash
python -m mineru.backend.rag.rag_analyze /path/to/doc.pdf ./output
```

---

## 3. 文件类型自动路由

### 4.1 自动分发

`dispatch_by_type()` 是统一入口，扫描输入路径，按文件后缀自动分发到对应处理器：

```python
from mineru.backend.rag.common import dispatch_by_type

# 自动路由: 目录里 PDF + Excel + DOCX 混在一起
result = await dispatch_by_type("/path/to/input_dir", "./output")
```

### 4.2 路由表

| 文件后缀 | 目标处理器 | 核心依赖 |
|---------|-----------|---------|
| `.pdf` | RAG Pipeline Chain | Azure DI + pypdfium2 |
| `.png` `.jpg` `.jpeg` `.webp` `.gif` `.bmp` `.tiff` | RAG Pipeline Chain | Azure DI (图片转 PDF) |
| `.xlsx` `.xls` `.xlsm` `.xltx` `.csv` | Excel Processor | openpyxl + Dify |
| `.docx` | Office 原生后端 | python-docx |
| `.pptx` | Office 原生后端 | pypptx |

### 4.3 文件扫描

```python
def collect_files_by_type(input_path: Path) -> dict[str, list[Path]]:
    """扫描输入路径, 按类型分组"""
    if input_path.is_file():
        return {detect_file_type(input_path): [input_path]}
    # 目录: 遍历所有文件
    groups = {}
    for path in sorted(input_path.glob("*")):
        groups.setdefault(detect_file_type(path), []).append(path)
    return groups
```

### 4.4 分发结果

```python
# 返回值
{
    "output_dir": "./output",
    "results": {
        "pdf":    [{"file": "report.pdf",  "status": "ok"}, ...],
        "excel":  [{"file": "data.xlsx",   "status": "ok"}, ...],
        "office": [{"file": "slides.pptx", "status": "ok"}, ...],
    },
    "summary": "5 files → pdf: 2/2, excel: 2/2, office: 1/1"
}
```

### 4.5 CLI 调用

```bash
# 自动检测类型
python -m mineru.backend.rag.common /path/to/input ./output

# 单个文件也自动路由
python -m mineru.backend.rag.common report.pdf ./output
python -m mineru.backend.rag.common data.xlsx ./output
```

### 4.6 输出目录结构 (多类型混入)

```
output/                          ← 统一输出根目录
├── .rag_cache/                  ← 共享缓存
│   └── {content_hash}/
│       ├── checkpoints/
│       ├── visualizations/
│       └── pipeline_run.json
├── report/                      ← PDF RAG 处理
│   └── rag_auto/
│       ├── report.md
│       ├── report_content_list.json
│       ├── report_middle.json
│       └── images/
├── data.md                      ← Excel 处理
├── data_sheets/
│   ├── Sheet1.md
│   └── Sheet2.md
├── slides/                      ← Office 处理
│   └── office/
│       ├── slides.md
│       └── slides_content_list.json
└── image.png.md                 ← 单图片处理
```

---

## 4. PDF 处理流程详解

### 4.1 完整阶段列表

```
Stage 1  → Stage 2  → Stage 3  → Stage 4  → Stage 5  → Stage 6  →
pdf_load  azure_di  page_group borderless image_filter table_merge

Stage 7    → Stage 8      → Stage 9         → Stage 10
dify_enhance hyperlink_map build_middle_json model_output
```

### 4.2 Stage 1: PDF Load — 文档加载

**职责**: 加载 PDF 字节流, 根据需要裁剪页范围。

**输入**: `ctx.pdf_bytes` (原始 PDF 字节流)

**输出**: `ctx.effective_pdf_bytes` (裁剪后的 PDF)

**逻辑**:
- 如果 `start_page_id > 0` 或 `end_page_id` 指定 → 用 `pypdfium2.import_pages()` 提取页范围(无损)
- 否则 → 直接使用原始 PDF

**配置**: `checkpoint=False` (不需要缓存, 操作极快)

### 4.3 Stage 2: Azure DI — Azure Document Intelligence 分析

**职责**: 将 PDF 发送到 Azure DI `prebuilt-layout` 模型, 获取结构化分析结果。

**输入**: `ctx.effective_pdf_bytes`

**输出**: `ctx.azure_result` (dict, 含 pages/paragraphs/tables/figures)

**Azure DI 返回的关键字段**:

| 字段 | 说明 |
|------|------|
| `pages` | 每页尺寸、行、词 |
| `paragraphs` | 段落内容、角色(title/sectionHeading/footnote等)、bbox |
| `tables` | 表格结构、单元格(row_index/col_index/row_span/col_span/content/kind) |
| `figures` | 图片/图表区域、caption、bbox |

**关键特性**: `cell.kind` 字段标注表头 (`"columnHeader"`), 用于后续表头去重。

### 4.4 Stage 3: Page Group — 页面分组

**职责**: 将 Azure DI 的扁平化结果按页码重组为页面级结构。

**输入**: `ctx.azure_result`

**输出**: `ctx.pages_data` (list[dict])

**输出结构**:

```python
[
  {
    "page_number": 0,
    "width": 8.5, "height": 11.0,
    "paragraphs": [
      {"content": "...", "role": "title", "bbox": [x0,y0,x1,y1]},
      ...
    ],
    "tables": [
      {"row_count": 5, "col_count": 3, "cells": [...], "table_html": "<table>..."},
    ],
    "figures": [
      {"bbox": [...], "caption": "...", "image_base64": "..."},
    ]
  },
  ...
]
```

**分组逻辑**:
- 每个 paragraph/table/figure 的 `bounding_regions[0].page_number` 决定归属页面
- 表格按第一个 cell 的页码归属
- 无页码信息的表格分配至第一页
- 支持 `start_page_id` / `end_page_id` 过滤

### 4.5 Stage 4: Borderless Table — 无框线表格检测

**职责**: 检测 PPT 转 PDF 等场景中 Azure DI 漏检的无框线表格。

**检测原理**:

```
1. Y 聚类 → 行 (ROW_Y_TOLERANCE=0.15 inch)
2. X 聚类 → 列模板 (COL_X_TOLERANCE=0.3 inch)
3. 对齐率检查 (> 70% → 判定为表格)
4. bbox 覆盖范围 → rowspan/colspan
5. 生成标准化 table dict
```

**触发条件**: Azure DI 未检测到表格 (page.tables 为空)

**阈值参数**: `MIN_ROWS=2`, `MIN_COLS=2`, `COLUMN_ALIGNMENT_RATIO=0.7`

### 4.6 Stage 5: Image Filter — 图片相关性过滤

**职责**: 在送 Dify 之前过滤无意义图片, 节省 API 调用并防止 Markdown 污染。

**九级过滤规则**:

| # | 规则 | 阈值 | 目标 |
|---|------|------|------|
| 1 | 绝对尺寸 | area < 0.1 sq in | 图标点 |
| 2 | 小尺寸+角落 | area < 0.5 & 边缘 8% | 角标 Logo |
| 3 | 跨页重复 | 同签名 ≥ 2 页 | PPT 模板元素 |
| 4 | 宽高比异常 | h/w < 0.15 或 > 6.0 | 分割线 |
| 5 | 无上下文+小尺寸 | area < 1.5 & 无引用词 | 孤立装饰 |
| 6 | 文本重叠 | ≥ 3 段落与图重叠 | 背景图 |
| 7 | 高覆盖率 | > 65% 页面 & 无引用 | 全页背景 |
| 8 | 嵌入物内容 | OCR 文本含邮件/聊天/水印模式 | 无关截图 |
| 9 | 内容孤立 | 无 caption & 无引用 & 中等尺寸 | 孤立嵌入 |

**可扩展检测器** (第 8 条):

```python
# 内置 7 类检测器
email       — 邮件截图 (From:/To:/@/Regards)
chat        — 聊天记录 (typing/message/delivered)
meeting     — 会议邀请 (Zoom/RSVP/Passcode)
watermark   — 水印标注 (CONFIDENTIAL/DRAFT/Approved)
social_media— 社交媒体 (followers/tweet/timeline)
code_terminal— 代码终端 (traceback/error/$ /Syntax)
advertisement— 广告 (Buy Now/Free Trial/subscribe)

# 通过 ~/mineru.json 扩展自定义
```

**过滤后的图片**: 仍保留在输出 Markdown 中 (`![](image.png)`), 只是不生成 LLM 描述。

### 4.7 Stage 6: Table Merge — 跨页表格合并

**职责**: 检测并合并跨页延续的表格, 处理表头去重。

**三种场景**:

```
场景 A: 每页有重复表头       场景 B: 仅首页有表头       场景 C: 混合
Page 1: [Header|Data]        Page 1: [Header|Data]     各页情况不同
Page 2: [Header|Data] 重复!   Page 2: [Data] 无表头     按各自标注分别处理
→ 移除 Page 2+ 的 Header     → 全部行追加 (无移除)
```

**合并逻辑**:

1. **延续检测**: 列数相同 + 列宽比例相似 (> 70%) + 表头签名相似 (Jaccard > 0.7) + 页面连续
2. **表头识别**: 仅信任 Azure DI `cell.kind == "columnHeader"` 显式标注, 不做猜测
3. **安全保护**: `rowspan > 1` 的表头行不删除 (延伸至数据行)
4. **列合并保护**: `colspan/rowspan` 属性在行号重映射时完整保留

### 4.8 Stage 7: Dify Enhancement — Dify 增强

**职责**: 调用 Dify Workflow 对图片生成描述, 对表格进行优化。

**处理逻辑**:

```
图片任务 (analyze_image):
  输入: image_base64, image_key, page_number, bbox, context_text
  输出: DifyImageResult (description/markdown, category, confidence)

表格任务 (optimize_table):
  输入: table_html, table_index, page_number, bbox, caption, context_text
  输出: DifyTableResult (optimized_html, optimized_markdown, caption, confidence)
```

**并发优化**:
- 图片和表格任务混合竞争 8 个并发槽 (非先后串行)
- 共享 `httpx.AsyncClient` 连接池 (最多 20 keep-alive, 50 最大连接)
- 最多 3 次重试, 指数退避 (1.5s / 3s / 4.5s)
- 单任务超时 120s

**容错**: Dify 未配置时自动跳过, 返回原始表格 HTML。

### 4.9 Stage 8: Hyperlink Map — 超链接回插

**职责**: 从原始 PDF 提取超链接 annotation, 按位置匹配到文本 span, 在 Markdown 中回插 `[text](url)`。

**处理流程**:

```
1. extract_pdf_links(pdf_bytes)
   pypdfium2 page.get_links() → PdfHyperlink(uri, page, bbox)
   坐标: PDF points ÷ 72 → inches

2. map_links_to_middle_json(middle_json, links)
   bbox 重叠率 > 0.3 → 候选匹配
   每链接仅标记最佳重叠 span
   多 span 重叠 → 合并文本, 其余标记 _hyperlink_merged

3. _lines_to_text() 渲染
   span._hyperlink → [text](url)
   span._hyperlink_merged → 跳过
```

### 4.10 Stage 9: Build Middle Json — middle_json 构建

**职责**: 将 `pages_data` + Dify 增强结果转换为标准 `middle_json` 格式。

**处理逻辑**:

```python
for page in pages_data:
    magic_model = RAGMagicModel(
        page_data,
        dify_image_results,   # 按 page_number 匹配
        dify_table_results,   # 按 page_number 匹配
    )
    preproc_blocks = magic_model.get_preproc_blocks()
    # 按阅读顺序排列: title → text → image → table → chart → equation → list

middle_json = {
    "pdf_info": [
        {
            "preproc_blocks": [...],
            "page_idx": 0,
            "page_size": [w, h],
            "discarded_blocks": [...],  # header/footer/page_number
        }
    ],
    "_backend": "rag",
    "_version_name": "3.1.x",
}
```

**后处理**: `finalize_middle_json()` 执行跨页表格合并 (此处在 middle_json 层面)。

### 4.11 Stage 10: Model Output — 输出模型元数据

**职责**: 构建 `model_output` (包含 Azure DI 元数据 + Dify 增强统计), 用于调试和追溯。

---

## 5. Excel 处理

### 5.1 独立处理链路

Excel 文件不经过 Azure DI (已经是结构化数据), 直接通过 openpyxl 读取, Dify 优化表格结构后输出 Markdown。

```
.xlsx 文件
    │
    ▼
┌──────────────────────┐
│  文件大小检测         │
│  < 50MB: normal 模式  │  ← 支持合并单元格
│  ≥ 50MB: read_only   │  ← 流式逐行, 低内存
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│  遍历所有 Sheet       │
│  每 Sheet → HTML 表格 │
└──────────┬───────────┘
           │
   ┌───────┼────────┐
   │       │        │
≤1000行 1000-5000  >5000行
   │       │        │
  Dify   Dify分块  跳过Dify
   │       │        │
   └───────┼────────┘
           │
           ▼
    Markdown 输出
```

### 5.2 使用方式

```python
# 方式 1: 自动路由 (推荐)
from mineru.backend.rag.common import dispatch_by_type
result = await dispatch_by_type("data.xlsx", "./output")

# 方式 2: 直接调用
from mineru.backend.rag.excel_processor import parse_excel_to_markdown
result = await parse_excel_to_markdown("data.xlsx", "./output")

# 方式 3: CLI
python -m mineru.backend.rag.excel_processor data.xlsx ./output

# 方式 4: Chain API
from mineru.backend.rag.pipeline.chain import excel_chain
ctx = await aio_doc_analyze_chain(
    pdf_bytes=open("data.xlsx", "rb").read(),
    chain=excel_chain(),
)
```

### 5.3 处理参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `MAX_FILE_SIZE_NORMAL_MODE` | 50 MB | 超过此值使用流式 read_only 模式 |
| `DIFY_MAX_ROWS_PER_SHEET` | 1000 | 单 Sheet 超此值分批送 Dify |
| `DIFY_SKIP_ROWS_THRESHOLD` | 5000 | 单 Sheet 超此值跳过 Dify |
| `MAX_ROWS_PER_CHUNK` | 500 | Dify 分块大小 |
| `MAX_COLS_FOR_MARKDOWN_TABLE` | 30 | 列数超此值降级为 HTML 输出 |

### 5.4 合并单元格处理

Normal 模式下检测 `ws.merged_cells`, 转为 HTML `rowspan`/`colspan`:

```python
# openpyxl 合并区域 → HTML 属性
merge_range = "A1:B2"  # 2行 × 2列
→ <td rowspan="2" colspan="2">合并内容</td>
```

### 5.5 大表格分块策略

```python
# 3000 行 Sheet → 分 3 块送 Dify (每块 1000 行)
# 第 1 块: rows[0:1000]   (含表头)
# 第 2 块: [表头] + rows[1000:2000]  (带表头保持列结构)
# 第 3 块: [表头] + rows[2000:3000]
# 最终合并: 去掉第 2/3 块的表头行, 拼接数据行
```

### 5.6 输出格式

```markdown
# data.xlsx

## Sheet1
*(150 rows × 12 cols, Dify optimized)*
| ID | Name   | Value | ...
|----|--------|-------|...
| 1  | Item A | 100   | ...

## Sheet2
*(300 rows × 8 cols)*
| Date       | Amount | ...
|------------|--------|...
| 2024-01-01 | 500    | ...

---
*3 sheets, 500 total rows, 3 Dify calls, 12.5s*
```

---

## 6. 配置体系

### 6.1 配置优先级

```
命令行参数 > 环境变量 > ~/mineru.json > 代码默认值
```

### 4.2 ~/mineru.json 配置

```json
{
  "models-dir": "/path/to/models",
  "latex-delimiter-config": {
    "display": {"left": "$$", "right": "$$"},
    "inline": {"left": "$", "right": "$"}
  },
  "rag_artifact_detectors": [
    {
      "category": "invoice",
      "label": "发票/报销凭证",
      "patterns": ["发票代码\\s*[:：]"],
      "keywords": ["发票", "报销", "增值税"],
      "min_matches": 2
    }
  ]
}
```

### 4.3 Pipeline Chain 配置 (`chain_config.json`)

```json
{
  "name": "my_pipeline",
  "stages": [
    {"name": "pdf_load",            "enabled": true, "checkpoint": false},
    {"name": "azure_di",            "enabled": true, "checkpoint": true},
    {"name": "page_group",          "enabled": true, "checkpoint": true},
    {"name": "borderless_table",    "enabled": true, "checkpoint": false,
     "params": {"ROW_Y_TOLERANCE": 0.2, "COL_X_TOLERANCE": 0.4}},
    {"name": "image_filter",        "enabled": true, "checkpoint": false},
    {"name": "table_merge",         "enabled": true, "checkpoint": false},
    {"name": "dify_enhance",        "enabled": true, "checkpoint": true,
     "params": {"DIFY_MAX_CONCURRENT": 12}},
    {"name": "hyperlink_map",       "enabled": true, "checkpoint": false},
    {"name": "build_middle_json",   "enabled": true, "checkpoint": true},
    {"name": "model_output",        "enabled": true, "checkpoint": false}
  ]
}
```

### 4.4 StageConfig 参数

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `enabled` | bool | true | 是否启用本阶段 |
| `checkpoint` | bool | true | 是否缓存中间结果 |
| `required` | bool | true | 失败是否中止 Pipeline |
| `timeout_s` | int | 0 | 超时秒数 (0=无限制) |
| `params` | dict | {} | 阶段特定参数 |

---

## 7. Pipeline Chain API

### 5.1 代码构建链

```python
from mineru.backend.rag.pipeline.chain import PipelineChain, default_rag_chain

# 获取默认链
chain = default_rag_chain()

# 修改链
chain.disable("dify_enhance")                # 禁用 Dify
chain.disable("image_filter")                # 禁用图片过滤
chain.insert_after("page_group", MyStage())  # 插入自定义阶段
chain.replace("table_merge", MyTableMerge()) # 替换阶段

# 查看链
print(chain.describe())
# PipelineChain:
#   1. [✓]    pdf_load
#   2. [✓] 💾 azure_di
#   3. [✓] 💾 page_group
#   4. [✓]    borderless_table
#   5. [✗]    image_filter
#   6. [✓]    table_merge
#   7. [✗]    dify_enhance
#   8. [✓]    hyperlink_map
#   9. [✓] 💾 build_middle_json
#  10. [✓]    model_output

# 执行
ctx = await chain.run(PipelineContext(pdf_bytes=..., output_dir="."))
```

### 5.2 预置链

```python
from mineru.backend.rag.pipeline.chain import (
    default_rag_chain,    # 完整链 (10 阶段)
    minimal_rag_chain,    # 最小链 (6 阶段, 无 Dify/图片过滤/超链接/无框线表格)
    office_rag_chain,     # Office 链 (关闭图片过滤)
)
```

### 5.3 通过名称列表构建

```python
chain = PipelineChain.from_names([
    "pdf_load",
    "azure_di",
    "page_group",
    "table_merge",         # 跳过 borderless_table, image_filter
    "build_middle_json",   # 跳过 dify_enhance, hyperlink_map
    "model_output",
])
```

### 5.4 通过配置文件构建

```python
chain = PipelineChain.from_config("pipelines/my_flow.json")
```

### 5.5 便捷函数

```python
from mineru.backend.rag.rag_analyze import aio_doc_analyze_chain

# 默认链
result = await aio_doc_analyze_chain(pdf_bytes, output_dir="./out")

# 链配置
result = await aio_doc_analyze_chain(
    pdf_bytes,
    chain_config="pipelines/my_flow.json",
)

# 名称列表
result = await aio_doc_analyze_chain(
    pdf_bytes,
    chain_names=["pdf_load", "azure_di", "page_group",
                 "table_merge", "build_middle_json"],
)
```

---

## 8. 扩展指南

### 6.1 添加自定义阶段

```python
from mineru.backend.rag.pipeline.stage import PipelineStage, StageConfig
from mineru.backend.rag.pipeline.context import PipelineContext
from mineru.backend.rag.pipeline.registry import StageRegistry

class MyCustomFilterStage(PipelineStage):
    name = "my_custom_filter"

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        # 从 ctx 读取数据
        pages = ctx.pages_data
        if not pages:
            return ctx

        # 自定义处理逻辑
        for page in pages:
            page["paragraphs"] = [
                p for p in page.get("paragraphs", [])
                if not self._is_noise(p)
            ]

        return ctx

    def _is_noise(self, paragraph: dict) -> bool:
        text = paragraph.get("content", "")
        return len(text.strip()) < 3  # 过滤过短段落

    def _build_output_summary(self, ctx):
        return {"filtered": True}

# 注册
StageRegistry().register("my_custom_filter", MyCustomFilterStage)

# 使用
chain = PipelineChain.from_names([
    "pdf_load", "azure_di", "page_group",
    "my_custom_filter",     # ← 自定义阶段
    "table_merge", "build_middle_json", "model_output",
])
```

### 6.2 添加自定义无关内容检测器

**方式 A: 代码注册**

```python
from mineru.backend.rag.image_relevance import (
    ArtifactDetector, ARTIFACT_DETECTORS
)

detector = ArtifactDetector(
    category="medical",
    label="医疗记录",
    patterns=[r'诊断\s*[:：]', r'处方\s*[:：]', r'患者\s*[:：]'],
    keywords=["病历", "就诊", "药方", "医嘱", "检验报告"],
    min_matches=2,
)
ARTIFACT_DETECTORS.append(detector)
```

**方式 B: 配置文件**

```json
// ~/mineru.json
{
  "rag_artifact_detectors": [
    {
      "category": "medical",
      "label": "医疗记录",
      "patterns": ["诊断\\s*[:：]", "处方\\s*[:：]"],
      "keywords": ["病历", "就诊", "药方"],
      "min_matches": 2
    }
  ]
}
```

**方式 C: 环境变量**

```bash
export MINERU_ARTIFACT_KEYWORDS="病历,就诊,药方,医嘱"
```

### 6.3 添加腾讯云/阿里云 OCR 替代 Azure DI

```python
class TencentOCRStage(PipelineStage):
    name = "tencent_ocr"

    async def execute(self, ctx):
        # 调用腾讯云 OCR API
        result = await tencent_client.recognize(ctx.effective_pdf_bytes)
        ctx.azure_result = self._convert_to_standard_format(result)
        return ctx

# 注册并替换
StageRegistry().register("tencent_ocr", TencentOCRStage)

chain = default_rag_chain().replace("azure_di", TencentOCRStage(
    config=StageConfig(params={"region": "ap-guangzhou"})
))
```

---

## 9. 可观测系统

### 7.1 PipelineTracker

`RAGPipelineTracker` 在 Pipeline 执行过程中记录:

```python
# 自动记录
tracker.start()                         # Pipeline 开始
tracker.start_stage("azure_di")         # 阶段开始
tracker.end_stage("azure_di", {...})    # 阶段结束
tracker.finish()                        # Pipeline 结束

# 生成的运行报告
pipeline_run.json = {
    "run_id": "a1b2c3d4e5f6",
    "doc_stem": "research_paper",
    "status": "completed",
    "total_duration_s": 18.45,
    "cache_hits": 2,
    "stages": {
        "azure_di": {
            "status": "completed", "duration_s": 8.2,
            "output_summary": {"pages": 50, "tables": 12}
        },
        "dify_enhance": {
            "status": "completed", "duration_s": 5.1,
            "output_summary": {"images": 15, "tables": 8}
        }
    }
}
```

### 7.2 检查点缓存

```
{output_dir}/.rag_cache/{content_hash}/
├── checkpoints/
│   ├── azure_result.json        ← Azure DI 全量结果
│   ├── pages_data.json          ← 页面分组结果
│   ├── dify_results.json        ← Dify 增强结果
│   └── middle_json.json         ← 最终 middle_json
├── visualizations/              ← 可视化产物
└── pipeline_run.json            ← 运行报告
```

**缓存恢复逻辑**: 如果某个阶段失败后重新运行, 已完成的阶段直接从缓存读取。

### 7.3 可视化产物

| 产物 | 说明 |
|------|------|
| `layout_page_N.png` | Azure DI 检测框线叠加 (绿=段落, 蓝=表格, 红=图片) |
| `dify_compare_*.txt` | Dify 增强前后对比 |
| `timeline.txt` | Pipeline 阶段耗时甘特图 |

---

## 10. 可视化看板

```bash
# 启动看板 (在 output 目录所在路径执行)
python -m mineru.backend.rag.dashboard --cache-dir ./output --port 8765

# 浏览器打开 http://localhost:8765
```

**看板功能**:
- 📊 运行列表选择器
- ✅ 状态卡片 (状态/总耗时/阶段进度/缓存命中)
- 📋 阶段流水线可视化 (绿=完成, 蓝=缓存, 红=失败, 灰=跳过)
- ⏱ 甘特图时间线
- 🖼 中间结果可视化画廊
- ⏱ 10s 自动刷新 (可暂停)

---

## 11. 环境变量参考

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `AZURE_DOC_INTELLIGENCE_ENDPOINT` | — | Azure DI 服务端点 |
| `AZURE_DOC_INTELLIGENCE_KEY` | — | Azure DI 访问密钥 |
| `DIFY_API_BASE_URL` | — | Dify 服务 URL |
| `DIFY_IMAGE_WORKFLOW_API_KEY` | — | Dify 图片 Workflow API Key |
| `DIFY_TABLE_WORKFLOW_API_KEY` | — | Dify 表格 Workflow API Key |
| `MINERU_LOG_LEVEL` | INFO | 日志级别 |
| `MINERU_TOOLS_CONFIG_JSON` | `~/mineru.json` | 配置文件路径 |
| `MINERU_ARTIFACT_KEYWORDS` | — | 自定义无关内容关键词 (逗号分隔) |
| `MINERU_ARTIFACT_PATTERNS` | — | 自定义无关内容 regex 模式 (逗号分隔) |
| `MINERU_API_MAX_CONCURRENT_REQUESTS` | 3 | API 最大并发请求数 |
| `MINERU_PROCESSING_WINDOW_SIZE` | 64 | 处理窗口大小 (每批页数) |
| `MINERU_FORMULA_ENABLE` | true | 启用公式识别 |
| `MINERU_TABLE_ENABLE` | true | 启用表格识别 |
| `MINERU_TABLE_MERGE_ENABLE` | true | 启用跨页表格合并 |

---

## 12. 常见问题

### Q: Dify 未配置时 Pipeline 如何工作？

Dify 未配置时, `DifyEnhanceStage` 自动跳过。表格保持 Azure DI 原始 HTML, 图片无 LLM 描述。

### Q: 如何处理超大 PDF (500+ 页)？

Azure DI 单次调用支持最多 2000 页, 处理时间约等于单页 * 并发度 (服务端内部并行)。无需客户端分片。

### Q: 缓存什么时候失效？

缓存基于 `MD5(pdf_bytes[:64KB] + params)` 的 content hash。如果 PDF 内容或参数改变, 缓存自动失效。

### Q: 如何只处理某个页范围？

```python
result = await aio_doc_analyze_chain(
    pdf_bytes,
    start_page_id=50, end_page_id=60,  # 仅处理第 51-61 页
)
```

PDF Load 阶段会用 `pypdfium2` 无损裁剪 PDF 后再送 Azure DI。

### Q: 如何禁用某个内置检测器？

```python
from mineru.backend.rag.image_relevance import ARTIFACT_DETECTORS

# 移除广告检测器
ARTIFACT_DETECTORS[:] = [
    d for d in ARTIFACT_DETECTORS
    if d.category != "advertisement"
]
```

### Q: 自定义阶段如何支持缓存恢复？

重写 `_restore_from_cache` 方法:

```python
class MyStage(PipelineStage):
    async def execute(self, ctx):
        ctx.my_result = heavy_computation(ctx.pages_data)
        return ctx

    def _restore_from_cache(self, ctx, cached):
        ctx.my_result = cached  # 恢复 ctx 状态
```

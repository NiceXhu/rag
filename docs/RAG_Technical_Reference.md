# RAG Pipeline 技术参考手册

> 每个阶段的详细处理流程、技术要点、数据结构、扩展指南

---

## 目录

- [Stage 1: PDF Load](#stage-1-pdf-load)
- [Stage 2: Azure DI](#stage-2-azure-di)
- [Stage 3: Page Group](#stage-3-page-group)
- [Stage 4: Borderless Table](#stage-4-borderless-table)
- [Stage 5: Image Filter](#stage-5-image-filter)
- [Stage 6: Table Merge](#stage-6-table-merge)
- [Stage 7: Dify Enhancement](#stage-7-dify-enhancement)
- [Stage 8: Hyperlink Map](#stage-8-hyperlink-map)
- [Stage 9: Build Middle JSON](#stage-9-build-middle-json)
- [Stage 10: Model Output](#stage-10-model-output)
- [附录 A: PipelineContext 完整字段](#附录-a-pipelinecontext-完整字段)
- [附录 B: 自定义 Stage 模板](#附录-b-自定义-stage-模板)

---

## Stage 1: PDF Load

### 职责

加载 PDF 字节流, 根据需要裁剪页范围。不涉及任何解析或 OCR。

### 处理流程

```
ctx.pdf_bytes (原始 PDF)
        │
        ▼
  ┌─────────────────────────┐
  │ start_page_id > 0       │──YES──→ pypdfium2.import_pages()
  │ or end_page_id set?     │         无损提取指定页 → 新 PDF bytes
  └────────┬────────────────┘
           │ NO
           ▼
    使用原始 pdf_bytes
           │
           ▼
  ctx.effective_pdf_bytes = 裁剪后的 PDF
```

### 技术要点

| 要点 | 说明 |
|------|------|
| **无损裁剪** | 使用 `pypdfium2.PdfDocument.import_pages()` 复制页面对象, 不是渲染为图片 |
| **坐标保持** | 裁剪后的 PDF 页面内部坐标不变, Azure DI 的 bbox 不需要重新映射 |
| **内存** | 裁剪操作在内存中完成 (`BytesIO`), 不写临时文件 |

### 关键代码路径

- `mineru/backend/rag/pipeline/stages/pdf_load.py`
- `mineru/utils/pdfium_guard.py::rewrite_pdf_bytes_with_pdfium()`

### 扩展指南

**添加加密 PDF 支持**:

```python
class PDFLoadStage(PipelineStage):
    name = "pdf_load"

    async def execute(self, ctx):
        # 检测加密
        if is_encrypted(ctx.pdf_bytes):
            password = ctx.params.get("pdf_password", "")
            ctx.effective_pdf_bytes = decrypt_pdf(ctx.pdf_bytes, password)
        else:
            ctx.effective_pdf_bytes = ctx.pdf_bytes
        return ctx
```

**添加 PDF 合并** (多文件输入):

```python
# 在 execute 中检测是否多个 PDF
if len(ctx.metadata.get("input_files", [])) > 1:
    ctx.effective_pdf_bytes = merge_pdfs(ctx.metadata["input_files"])
```

---

## Stage 2: Azure DI

### 职责

将 PDF 发送到 Azure Document Intelligence `prebuilt-layout` 模型, 获取结构化分析结果。

### 处理流程

```
ctx.effective_pdf_bytes
        │
        ▼
  ┌─────────────────────────────────────┐
  │ AzureDocumentIntelligenceClient     │
  │ .analyze_document(bytes, "application/pdf") │
  └──────────────┬──────────────────────┘
                 │
                 ▼
  ┌─────────────────────────────────────┐
  │ Azure 服务端内部处理:               │
  │ 1. PDF 解析 (多页自动并行)           │
  │ 2. Layout 分析 (段落/表格/图片)       │
  │ 3. OCR (文本提取 + 位置)             │
  │ 4. 阅读顺序                          │
  └──────────────┬──────────────────────┘
                 │
                 ▼
  ctx.azure_result = {
      "pages":       [page_info, ...],
      "paragraphs":  [para_info, ...],
      "tables":      [table_info, ...],
      "figures":     [figure_info, ...],
      "sections":    [section_info, ...],
      "metadata":    {...},
  }
```

### 关键数据结构

**Page**:
```python
{
    "page_number": 1,        # 1-based
    "width": 8.5, "height": 11.0,  # inches
    "unit": "inch",
    "lines": [{"content": "...", "polygon": [...], "words": [...]}]
}
```

**Paragraph**:
```python
{
    "content": "段落文本",
    "role": "title" | "sectionHeading" | "pageHeader" | "pageFooter" | "footnote" | None,
    "page_number": 3,
    "bbox": [x0, y0, x1, y1],  # inches
}
```

**Table Cell** (核心 — 结构最丰富的元素):
```python
{
    "row_index": 2, "col_index": 0,
    "row_span": 2, "col_span": 1,   # 合并单元格!
    "content": "合并单元格内容",
    "kind": "columnHeader" | "rowHeader" | "columnFooter" | "",  # ★ 表头标注
    "page_number": 3,
    "bbox": [x0, y0, x1, y1],
}
```

**Figure**:
```python
{
    "id": "figure.1",
    "page_numbers": [3],
    "bbox": [x0, y0, x1, y1],
    "caption": "Figure 1: ...",
    "footnotes": ["...", "..."],
    "image_base64": "data:image/png;base64,...",  # 如果启用图片提取
}
```

### 技术要点

| 要点 | 说明 |
|------|------|
| **单次调用** | 整个 PDF 一次发送, Azure 内部自动并行各页 |
| **坐标单位** | Azure DI 返回 inches, pypdfium2 返回 points (÷72 转换) |
| **cell.kind** | `"columnHeader"` 是后续表头去重的唯一可靠信号 |
| **API 版本** | `2024-07-31-preview` — 支持 figures 和 markdown 输出 |
| **重试** | 最多 3 次, 指数退避 2s/4s/8s |
| **并发控制** | `asyncio.Semaphore(4)` 限制同时请求数 |
| **页码** | Azure DI page_number 从 1 开始; RAG 内部统一 0-based |

### 扩展指南

**替换为腾讯云 OCR**:

```python
class TencentOCRStage(PipelineStage):
    name = "tencent_ocr"

    async def execute(self, ctx):
        result = await tencent_client.recognize(ctx.effective_pdf_bytes)
        ctx.azure_result = self._adapt_to_standard_format(result)
        return ctx

    def _adapt_to_standard_format(self, raw):
        # 将腾讯云返回格式转为 Azure DI 兼容格式
        # 关键: 保持 pages/paragraphs/tables/figures 结构一致
        ...
```

**支持多页并发** (超大 PDF 优化):

```python
# 将 2000+ 页 PDF 分为 4 个 500 页块, 并发发送
chunks = split_pdf(ctx.effective_pdf_bytes, chunk_size=500)
results = await asyncio.gather(*[
    azure_client.analyze_document(chunk) for chunk in chunks
])
ctx.azure_result = merge_results(results)
```

---

## Stage 3: Page Group

### 职责

将 Azure DI 返回的扁平化 (paragraphs/tables/figures 混在一起) 结果按页码重组成页面级结构。

### 处理流程

```
ctx.azure_result
  ├── pages: [page1, page2, ...]
  ├── paragraphs: [para1(pg3), para2(pg1), para3(pg3), ...]
  ├── tables: [table1(pg2,pg3), table2(pg1), ...]
  └── figures: [fig1(pg3), fig2(pg1), ...]
           │
           ▼
  _group_azure_results_by_page()
           │
    遍历每个 paragraph/table/figure
    读取 bounding_regions[0].page_number
    分配到对应 page_map[page_number]
           │
           ▼
  ctx.pages_data = [
    {
      "page_number": 0,         # 0-based
      "width": 8.5, "height": 11.0,
      "paragraphs": [para_on_page_0, ...],
      "tables":     [table_on_page_0, ...],
      "figures":    [figure_on_page_0, ...],
    },
    ...
  ]
```

### 分组规则

| 元素 | 归属逻辑 |
|------|---------|
| Paragraph | `bounding_regions[0].page_number` 直接归属 |
| Table | 取第一个 cell 的 `page_number`; 无页码信息 → 归入第一页 |
| Figure | `bounding_regions[0].page_number` 直接归属 |
| 跨页表格 | 一个表格可能出现在多页 (page_numbers 列表); 归入首页 |

### 技术要点

| 要点 | 说明 |
|------|------|
| **页码映射** | Azure DI 1-based → 内部 0-based; `page_number = azure_page - 1` |
| **空页处理** | 如果某页无任何元素, 仍创建 page entry (保持页码连续性) |
| **页范围过滤** | 支持 `start_page_id` / `end_page_id` 切片 |

### 扩展指南

**添加章节分组** (按 Azure DI sections 重组织):

```python
class SectionGroupStage(PipelineStage):
    name = "section_group"

    async def execute(self, ctx):
        sections = ctx.azure_result.get("sections", [])
        # 利用 sections 的 element 引用关系
        # 按章节重新组织 pages_data
        ...
```

---

## Stage 4: Borderless Table

### 职责

检测 PPT 转 PDF 等场景中 Azure DI 漏检的无框线表格 (文本按行列排列但无明显表格框线)。

### 处理流程

```
    页面上的 paragraphs (Azure DI 未检出表格)
                │
                ▼
    ┌──────────────────────────┐
    │ 1. Y 坐标聚类 → 行        │
    │    ROW_Y_TOLERANCE=0.15" │
    │    → rows: [(y, indices)] │
    └──────────┬───────────────┘
               │
               ▼
    ┌──────────────────────────┐
    │ 2. X 坐标聚类 → 列模板    │
    │    COL_X_TOLERANCE=0.3"  │
    │    → col_centers: [x0,x1]│
    └──────────┬───────────────┘
               │
               ▼
    ┌──────────────────────────┐
    │ 3. 对齐率检查             │
    │    aligned / total ≥ 70% │
    │    → 非表格(跳过) / 表格   │
    └──────────┬───────────────┘
               │
               ▼
    ┌──────────────────────────┐
    │ 4. 构建 cell 网格         │
    │    grid[row][col] = text  │
    │    bbox 覆盖 → rowspan    │
    └──────────┬───────────────┘
               │
               ▼
    ┌──────────────────────────┐
    │ 5. 生成 table dict        │
    │    (兼容 Azure DI 格式)    │
    └──────────────────────────┘
```

### 关键参数

| 参数 | 默认值 | 调整建议 |
|------|--------|---------|
| `ROW_Y_TOLERANCE` | 0.15" | 松散排版增大到 0.25" |
| `COL_X_TOLERANCE` | 0.3" | 宽列文档增大到 0.5" |
| `COLUMN_ALIGNMENT_RATIO` | 0.7 | 严格对齐提高到 0.85 |
| `MIN_ROWS / MIN_COLS` | 2 | 最小表格维度 |

### 扩展指南

**添加视觉特征检测** (检测虚线/浅色表格线):

```python
async def execute(self, ctx):
    for page in ctx.pages_data:
        # 渲染页面图片 (低分辨率, 仅用于线检测)
        img = render_page(ctx.pdf_bytes, page["page_number"], dpi=72)
        # OpenCV 检测水平/垂直线
        lines = detect_grid_lines(img)
        if lines:
            # 基于线检测重建表格结构
            tables = build_tables_from_lines(lines, page["paragraphs"])
            page["tables"].extend(tables)
```

---

## Stage 5: Image Filter

### 职责

在送 Dify 之前过滤无意义图片 (装饰元素/背景/图标/无关截图), 节省 API 调用并防止 Markdown 污染。

### 处理流程

```
    所有 figures (来自 Azure DI)
                │
        ┌───────┴────────┐
        │  第一遍扫描       │
        │  收集尺寸签名     │
        │  检测模板重复     │
        └───────┬────────┘
                │
        ┌───────┴────────┐
        │  第二遍逐图评估   │
        │  九级漏斗过滤     │
        └───────┬────────┘
                │
    标记 figure._skip_dify = True/False
```

### 九级过滤规则详情

```
Rule 1: 绝对尺寸                   area < 0.1 sq"
  → 跳过 (一定是图标/点)

Rule 2: 小尺寸 + 角落位置           area < 0.5 & 边缘 8%
  → 跳过 (角标/Logo)

Rule 3: 跨页重复                   同签名的图 ≥ 2 页
  → 跳过 (PPT 模板元素)
  签名: MD5(量化到 0.1" 的宽×高)

Rule 4: 宽高比异常                 h/w < 0.15 或 > 6.0
  → 跳过 (分割线/装饰条)

Rule 5: 无上下文 + 小尺寸           area < 1.5 & 无引用词
  → 跳过 (孤立装饰)
  引用词: "图","如图","figure","shown",...

Rule 6: 文本重叠                   ≥ 3 段落 bbox 与图片重叠
  → 跳过 (背景图 — 文字在图上)

Rule 7: 高页面覆盖率               > 65% 页面 & 无引用
  → 跳过 (全页背景)

Rule 8: 嵌入物内容检测              OCR 文本匹配无关模式
  → 跳过 (邮件/聊天/水印/广告...)
  使用可插拔 ArtifactDetector 框架

Rule 9: 内容孤立                   无 caption & 无引用 & 中等尺寸
  → 跳过 (孤立嵌入)
```

### 内置 ArtifactDetector 列表

| category | 目标 | 关键模式 | min_matches |
|----------|------|---------|-------------|
| email | 邮件截图 | From:, @, Regards | 2 |
| chat | 聊天记录 | typing..., delivered, WhatsApp | 2 |
| meeting | 会议邀请 | Zoom, RSVP, Passcode | 2 |
| watermark | 水印 | CONFIDENTIAL, DRAFT | 1 |
| social_media | 社交媒体 | followers, tweet, timeline | 2 |
| code_terminal | 代码终端 | traceback, $, error: | 2 |
| advertisement | 广告 | Buy Now, Free Trial, Subscribe | 2 |

### 技术要点

| 要点 | 说明 |
|------|------|
| **不对比像素** | 所有检测基于 bbox 尺寸/位置 + OCR 文本, 不解码 base64 |
| **签名去重** | 尺寸签名 = `MD5(quantized_width × quantized_height)` — 不需要图片内容 |
| **热加载** | `reload_detectors()` 在每次 Pipeline 启动时从 `~/mineru.json` 和环境变量重新加载 |
| **过滤后行为** | 图片仍保留在输出 `![](path)`, 只是无 LLM 描述 |

### 扩展指南

**方式 A: 配置文件添加检测器**

```json
// ~/mineru.json
{
  "rag_artifact_detectors": [
    {
      "category": "my_domain",
      "label": "特定领域无关内容",
      "patterns": ["模式1", "模式2"],
      "keywords": ["关键词1", "关键词2"],
      "min_matches": 2
    }
  ]
}
```

**方式 B: 代码注册检测器**

```python
from mineru.backend.rag.image_relevance import ArtifactDetector, ARTIFACT_DETECTORS

ARTIFACT_DETECTORS.append(ArtifactDetector(
    category="handwritten",
    label="手写批注",
    patterns=[r'批注\s*[:：]', r'修改意见\s*[:：]'],
    keywords=["手写", "批注", "修改", "删除"],
    min_matches=2,
))
```

**方式 C: 环境变量快速添加**

```bash
export MINERU_ARTIFACT_KEYWORDS="发票,报销,审批单,合同编号"
```

**方式 D: 替换整个过滤逻辑**

```python
class MLBasedFilterStage(PipelineStage):
    name = "ml_image_filter"
    async def execute(self, ctx):
        model = load_image_classifier()  # 自己的分类模型
        for page in ctx.pages_data:
            for fig in page.get("figures", []):
                img = decode_base64(fig["image_base64"])
                score = model.predict(img)
                if score < 0.5:
                    fig["_skip_dify"] = True
        return ctx
```

---

## Stage 6: Table Merge

### 职责

处理跨页表格: 检测延续关系, 保持按页存储, 为缺失表头的续页补全表头。

### 两种模式

```
Mode: "complete" (默认)              Mode: "merge"
══════════════════                   ═══════════════
Page 1  [Header|Data]               Page 1  [合并后的大表格]
Page 2  [Header|Data] → 去重        Page 2  (空)
Page 3  [Data]        → 补全表头    Page 3  (空)
  ↓                                   ↓
每页独立存储                        所有行合并到首页
每页都有表头                         只有一个逻辑表格
```

### 处理流程 (complete 模式)

```
所有表格 (按页面)
        │
        ▼
┌──────────────────────────────┐
│ _detect_continuation_groups()│
│                              │
│ 两表是否为延续?               │
│ 1. 列数相同                   │
│ 2. 列宽比例相似 (>70%)         │
│ 3. 表头签名相似 (Jaccard>0.7)  │
│ 4. 页面连续 (prev.max+1==cur) │
└──────────────┬───────────────┘
               │
               ▼
  complete_table_headers(pages_data)
               │
               ▼
┌──────────────────────────────┐
│ 对每个延续组:                  │
│                              │
│ 首页表格                      │
│   _extract_header_cells()    │
│   ├ 优先: kind="columnHeader" │
│   └ 回退: row_index == 0     │
│   → header_cells (带 col/row_span)│
│                              │
│ 续页表格 (pages 2+)           │
│   cont_headers = _detect_header_rows()
│   ├ 有显式表头 → 跳过          │
│   └ 无显式表头 → _prepend_header_to_table()
│       ├ 复制首页 header_cells │
│       ├ 所有现有 cell row_index += N│
│       ├ 插入 header cells (row 0,1,...)│
│       ├ 保留 col_span/row_span│
│       └ 重新生成 table_html   │
└──────────────────────────────┘
```

### 核心函数签名

```python
def complete_table_headers(pages_data: list[dict]) -> list[dict]:
    """
    跨页表格表头补全 — 保持按页存储, 只为缺失表头的续页补全。
    不做表格合并 — 每页仍然有独立的 table dict。
    """

def _extract_header_cells(table: dict) -> list[dict]:
    """
    从表格提取表头 cells。
    优先级: Azure DI kind="columnHeader" > 第一行回退
    """

def _prepend_header_to_table(table: dict, header_cells: list[dict]) -> dict:
    """
    在表格数据行之前插入表头行。
    - 所有现有 cell.row_index += len(header_rows)
    - 插入 header cells (row_index 保持原始值)
    - 重新计算 row_count
    - 重新生成 table_html
    - 设置 _header_completed = True
    """

def _detect_continuation_groups(tables: list[dict]) -> list[list[dict]]:
    """
    检测跨页延续组。
    返回分组列表, 每组是一系列按页序排列的延续表格。
    """
```

### 表头检测逻辑

```python
# 三层策略
# 1. Azure DI kind 标注 (最可靠)
header_cells = [c for c in cells if c["kind"] == "columnHeader"]

# 2. 回退: 没有标注 → 第一行
if not header_cells:
    first_row = min(c["row_index"] for c in cells)
    header_cells = [c for c in cells if c["row_index"] == first_row]

# 3. rowspan > 1 的表头不删除 (延伸到数据行)
if any(c["row_span"] > 1 for c in header_cells):
    safe_headers.add(row)  # 保留
```

### 技术要点

| 要点 | 说明 |
|------|------|
| **合并单元格保护** | cell 的 `col_span`/`row_span` 在行号重映射时完整保留 |
| **表头恢复权重** | `kind` > 第一行回退 > 不猜测 |
| **页面连续性** | `prev.max_page_number + 1 == cur.min_page_number` |

### 扩展指南

**切换到 merge 模式**:

```python
chain = default_rag_chain()
chain.replace("table_merge", TableMergeStage(
    config=StageConfig(params={"mode": "merge"})
))
```

**添加更智能的表头检测** (基于内容特征):

```python
def detect_header_by_content(cells):
    """基于内容的表头检测: 短文本/粗体/全大写"""
    for cell in cells:
        text = cell.get("content", "")
        if len(text) < 30 and text.isupper():
            cell["kind"] = "columnHeader"
```

**添加表尾 (footer) 检测**:

```python
# 检测 "合计"/"Total"/"小计" 行作为表尾
footer_keywords = ["合计", "总计", "total", "sum", "小计"]
for cell in cells:
    if any(kw in cell["content"].lower() for kw in footer_keywords):
        cell["kind"] = "columnFooter"
```

---

## Stage 7: Dify Enhancement

### 职责

调用 Dify Workflow 对图片生成 LLM 描述, 对表格进行 LLM 结构优化。

### 处理流程

```
pages_data (已过滤/合并)
        │
        ▼
┌──────────────────────────────┐
│ _build_dify_tasks()           │
│                              │
│ 遍历每页:                     │
│  ├ figures (跳过 _skip_dify)  │
│  │   → analyze_image() task  │
│  └ tables                    │
│      → optimize_table() task │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│ _execute_dify_enhancement()   │
│                              │
│ 图片+表格任务合并为一个池      │
│ asyncio.gather 分批 (16/批)   │
│ Semaphore(8) 控制并发         │
│ 共享 httpx.AsyncClient 连接池 │
└──────────────┬───────────────┘
               │
               ▼
ctx.dify_image_results = [DifyImageResult, ...]
ctx.dify_table_results = [DifyTableResult, ...]
```

### 图片增强

```
Dify Workflow 输入:
  image_base64: "data:image/png;base64,..."
  image_key: "fig_p3_0"
  page_number: 3
  bbox: [2.0, 4.5, 5.0, 7.2]
  context_text: "周围段落的文本..."

Dify Workflow 输出:
  description: "该图展示了2024年Q4的销售数据..."
  category: "chart" | "image"
  confidence: 0.92
```

### 表格优化

```
Dify Workflow 输入:
  table_html: "<table>...</table>"
  table_index: 2
  page_number: 5
  caption: "Table 3: 实验结果"
  context_text: "前后段落..."

Dify Workflow 输出:
  optimized_html: "<table>...</table>"  (结构优化后)
  optimized_markdown: "| col1 | col2 |\n|..."  (可选)
  caption: "Table 3: 实验结果 (单位: ms)"
  confidence: 0.88
```

### 技术要点

| 要点 | 说明 |
|------|------|
| **连接池复用** | `httpx.AsyncClient` 单例, keep-alive 20 连接, 避免每次 TCP+TLS |
| **混合并发** | 图片和表格任务混合竞争 8 个槽位, 不互相等待 |
| **分批执行** | 每 16 个任务一批 `asyncio.gather`, 避免过多并发 |
| **重试** | 3 次, 指数退避 1.5s/3s/4.5s |
| **超时** | 单任务 120s (LLM 推理可能慢) |
| **容错** | 单任务失败不影响其他; Dify 未配置时整个阶段跳过 |

### 扩展指南

**替换为其他 LLM 服务**:

```python
class OpenAIEnhanceStage(PipelineStage):
    name = "openai_enhance"

    async def execute(self, ctx):
        client = AsyncOpenAI()
        for page in ctx.pages_data:
            for fig in page.get("figures", []):
                desc = await client.chat.completions.create(
                    model="gpt-4o",
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Describe this image"},
                            {"type": "image_url", "image_url": {"url": fig["image_base64"]}},
                        ]
                    }]
                )
                fig["_llm_description"] = desc.choices[0].message.content
        return ctx
```

**添加代码块增强**:

```python
class CodeEnhanceStage(PipelineStage):
    name = "code_enhance"
    async def execute(self, ctx):
        for page in ctx.pages_data:
            for block in page.get("paragraphs", []):
                if detect_code_block(block["content"]):
                    optimized = await dify_client.analyze_code(block["content"])
                    block["content"] = optimized
        return ctx
```

---

## Stage 8: Hyperlink Map

### 职责

从原始 PDF 提取超链接 annotation, 按 bbox 位置匹配到 middle_json 的 span, 在 Markdown 中渲染 `[text](url)`。

### 处理流程

```
原始 PDF (ctx.pdf_bytes)
        │
        ▼
┌──────────────────────────┐
│ extract_pdf_links()       │
│ pypdfium2: page.get_links()│
│ PDF points ÷ 72 → inches  │
│ → [PdfHyperlink, ...]     │
└──────────┬───────────────┘
           │
           ▼
┌──────────────────────────┐
│ map_links_to_middle_json()│
│                          │
│ 对每个 TEXT span:         │
│   span.bbox ∩ link.bbox  │
│   重叠率 > 0.3 → 候选     │
│                          │
│ 去重:                     │
│   每 link 只取最佳 span   │
│   多 span 重叠 → 合并文本 │
│   其余标记 _hyperlink_merged│
└──────────┬───────────────┘
           │
           ▼
┌──────────────────────────┐
│ _lines_to_text() 渲染     │
│ span._hyperlink          │
│   → [text](url)           │
│ span._hyperlink_merged   │
│   → 跳过                  │
└──────────────────────────┘
```

### 坐标转换

```
pypdfium2 points           Azure DI inches           判断
═══════════════            ══════════════            ════
bbox = [144,720,288,740]   bbox = [2.0,10.0,4.0,10.28]
        │
   ÷ 72 ┘
```

### 技术要点

| 要点 | 说明 |
|------|------|
| **匹配精度** | `_bbox_overlap_ratio` 使用 `min(area_a, area_b)` 归一化, 偏向小面积 |
| **表格去重** | 同链接匹配多个 span → 文本合并到最佳 span, 其余标记 `_hyperlink_merged` |
| **坐标系统** | PDF points (1/72 inch) → inches (÷72) |

### 扩展指南

**添加内部文档链接** (PDF 目录跳转):

```python
def extract_internal_links(pdf_bytes):
    """提取 PDF 内部跳转 (GoTo 类型的 link)"""
    for link in page.get_links():
        if link.get("kind") == "goto":
            dest_page = link.get("page")
            yield InternalLink(source_page, dest_page, bbox)
```

**添加脚注/尾注链接**:

```python
# 检测 superscript 数字 + 对应页面底部 footnote
# 建立双向链接: body[1] ↔ footnote[1]
```

---

## Stage 9: Build Middle JSON

### 职责

将 pages_data + Dify 增强结果转换为 MinerU 标准的 middle_json 格式。

### 处理流程

```
pages_data + dify_image_results + dify_table_results
        │
        ▼
┌──────────────────────────────┐
│ RAGMagicModel (逐页)          │
│                              │
│ 解析 paragraph → TEXT/TITLE   │
│ 解析 table → TABLE_BODY       │
│   + Dify optimized_html      │
│ 解析 figure → IMAGE/CHART     │
│   + Dify description → caption│
│                              │
│ 按阅读顺序排列 blocks          │
│ 丢弃: HEADER/FOOTER/PAGE_NUM │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│ middle_json = {               │
│   "pdf_info": [               │
│     {                         │
│       "preproc_blocks": [...],│
│       "page_idx": 0,          │
│       "page_size": [w, h],    │
│       "discarded_blocks": [...],│
│     },                        │
│   ],                          │
│   "_backend": "rag",          │
│ }                             │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│ finalize_middle_json()        │
│ - 跨页表格合并 (middle_json层)│
└──────────────────────────────┘
```

### BlockType 映射

```
Azure DI role                 → BlockType
══════════════                ═════════
"title" / "sectionHeading"    → TITLE
"pageHeader"                  → HEADER (丢弃)
"pageFooter"                  → FOOTER (丢弃)
"pageNumber"                  → PAGE_NUMBER (丢弃)
"footnote"                    → PAGE_FOOTNOTE (丢弃)
"formulaBlock"                → INTERLINE_EQUATION
(无 role, 普通文本)            → TEXT
```

### 技术要点

| 要点 | 说明 |
|------|------|
| **Dify 增强注入** | `dify_image_results` 的 description 写入对应 image block 的 caption span |
| **表格 HTML** | `dify_table_results` 的 optimized_html 替换原始 table_html |
| **阅读顺序** | 按 bbox y 坐标排序 (y 优先, x 次之) |
| **空 span 处理** | content 为空的 span 跳过不输出 |

### 扩展指南

**添加自定义 BlockType**:

```python
# 在 RAGMagicModel._process() 中添加
elif self._is_callout_block(block):
    block["type"] = "callout"  # 自定义类型
    block["callout_type"] = "info" | "warning" | "tip"

# 在 _lines_to_text() 中添加渲染
elif block_type == "callout":
    return f"> **{block['callout_type'].upper()}:** {text}"
```

**添加阅读顺序优化** (基于文档布局而非纯 y 坐标):

```python
def optimize_reading_order(blocks):
    """使用 XY-cut 算法或基于列的阅读顺序"""
    columns = detect_columns(blocks)
    for col in columns:
        col.sort(key=lambda b: b["bbox"][1])  # 列内按 y 排序
    return interleave_columns(columns)
```

---

## Stage 10: Model Output

### 职责

构建 model_output (Azure DI 元数据 + Dify 增强统计), 用于调试追溯。

### 输出结构

```python
ctx.model_output = {
    "backend": "rag",
    "azure_di": {
        "page_count": 50,
        "metadata": {"model_id": "prebuilt-layout", "api_version": "2024-07-31-preview"},
    },
    "dify_enhancement": {
        "image_count": 15,
        "table_count": 8,
        "enhanced_images": [
            {"image_key": "fig_p3_0", "page_number": 3, "description_length": 120, "category": "chart"},
            ...
        ],
        "enhanced_tables": [
            {"table_index": 2, "page_number": 5, "has_optimized_md": true},
            ...
        ],
    },
    "timestamp": 1717300000.0,
}
```

---

## 附录 A: PipelineContext 完整字段

```python
@dataclass
class PipelineContext:

    # ── 输入 (创建时设置) ──
    pdf_bytes: bytes                        # 原始文件字节流
    output_dir: str = "."                   # 输出目录
    doc_stem: str = "document"              # 文档标识 (用于文件名和缓存)
    params: dict = {}                       # 全局参数

    # ── 中间结果 (各阶段按顺序填充) ──
    effective_pdf_bytes: bytes | None = None   # Stage 1: PDF Load
    azure_result: dict | None = None           # Stage 2: Azure DI
    pages_data: list[dict] | None = None       # Stage 3: Page Group
    #                                            Stage 4-6: 原地修改 pages_data
    dify_image_results: list = []              # Stage 7: Dify Enhancement
    dify_table_results: list = []
    middle_json: dict | None = None            # Stage 9: Build Middle JSON
    model_output: dict | None = None           # Stage 10: Model Output

    # ── 元数据 ──
    tracker: RAGPipelineTracker | None = None  # 可观测追踪器
    metadata: dict = {}                        # 自由扩展
    errors: list[dict] = []                    # 错误收集
```

---

## 附录 B: 自定义 Stage 模板

### 完整模板

```python
from mineru.backend.rag.pipeline.stage import PipelineStage, StageConfig
from mineru.backend.rag.pipeline.context import PipelineContext


class MyCustomStage(PipelineStage):
    name = "my_custom_stage"
    """
    自定义阶段的唯一标识。

    用于:
    - PipelineChain 名称引用
    - StageRegistry 注册
    - Checkpoint 缓存 key
    - 日志和追踪
    """

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        """
        核心处理逻辑。从 ctx 读取上游数据, 处理后写回 ctx。

        Args:
            ctx: 当前 Pipeline 状态

        Returns:
            修改后的 ctx (可返回同一个对象或新对象)

        Raises:
            任何异常 — 如果 config.required=True (默认),
            Pipeline 将在此中止。
        """

        # ── 读取上游数据 ──
        data = ctx.pages_data
        if not data:
            # 可以检查前置条件
            ctx.add_error(self.name, "pages_data is empty", fatal=True)
            return ctx

        # ── 核心逻辑 ──
        for page in data:
            # ... 你的处理逻辑
            pass

        # ── 写入结果 ──
        ctx.pages_data = data  # 写回修改

        return ctx

    def _build_output_summary(self, ctx: PipelineContext) -> dict:
        """构建输出摘要 (显示在 pipeline_run.json 和日志中)"""
        return {"processed": len(ctx.pages_data or [])}

    def _restore_from_cache(self, ctx: PipelineContext, cached) -> None:
        """
        从 checkpoint 恢复 ctx 状态。

        Args:
            cached: save_checkpoint 时保存的数据
        """
        ctx.pages_data = cached

    # 可选: 覆写 can_skip
    def can_skip(self, ctx: PipelineContext) -> bool:
        """返回 True 则跳过本阶段"""
        return ctx.params.get("skip_my_stage", False)
```

### 注册和使用

```python
from mineru.backend.rag.pipeline.registry import StageRegistry

# 注册
StageRegistry().register("my_custom_stage", MyCustomStage)

# 使用
from mineru.backend.rag.pipeline.chain import PipelineChain

chain = PipelineChain.from_names([
    "pdf_load", "azure_di", "page_group",
    "my_custom_stage",       # ← 自定义阶段
    "build_middle_json", "model_output",
])

ctx = await chain.run(PipelineContext(pdf_bytes=pdf_bytes, output_dir="./out"))
```

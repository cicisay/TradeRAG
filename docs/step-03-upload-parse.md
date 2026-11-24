# 步骤 3 实施记录：文档上传 + 多格式解析（M2 第一部分）

日期：2026-11-09　对应里程碑：M2　对应 PRD：5.1 FR-101/109 / 6.1 数据模型 / 7.2 上传校验

## 本步做了什么

### 1. 数据模型（app/models/document.py）
- `document`：户口本（kb_id / doc_name / status / current_version_id）
- `doc_version`：版本链（FR-109），含 checksum(SHA-256)、file_path、raw_text、page_count、parse_status
- `document_meta`：语种(JSON) / **biz_group（随知识库，用户不可自选，防越权标注）** / sensitivity / doc_type(5 类) / authority_level(0=T0..3=T3) / effective_date / expire_date
- 状态机：uploaded → parsing → parsed / failed

### 2. 解析服务（app/services/parser.py）
- 白名单扩展名：txt / md / pdf / docx / xlsx
- txt/md 兼容 UTF-8 与 GBK；pdf 用 PyMuPDF 取文本层；docx 含表格逐行拼接；xlsx 逐 sheet
- 解析异常统一兜底返回错误描述，不崩溃（FR-216 精神）
- 扫描件（提取不到文字）→ parse failed；OCR 通道在下一子步骤接入

### 3. 上传接口（app/api/documents.py）
- POST /api/v1/documents/upload（kb:manage 权限）+ GET 列表 / GET 详情（get_current_user）
- 安全校验链：扩展名白名单 → **魔数校验**（%PDF-/PK）→ **宏检测**（vbaProject.bin 即拒）→ ≤100MB → 空文件拒
- 业务组越权校验（FR-603）：非管理员只能操作本组知识库；跨组访问详情/列表均拦截
- checksum 全文 SHA-256；文件落盘 storage_dir/files/{kb}/{doc}/v1_{name}

### 4. 测试（17/17 全过）
- 新增 8 用例：上传成功/普通用户 403/exe 拒/假 PDF 魔数拒/含宏 docx 拒/真 PDF 解析页数/跨组上传 403/跨组列表隔离
- 夹具扩展：knowledge_a(biz_a)、knowledge_b(biz_b) 两知识管理员 + 两组各一知识库

## 自查发现并修复的 Bug（1 个）

1. **PyMuPDF 重复关闭文档**：`with pymupdf.open(...)` 块内残留 `pdf.close()`，块退出时二次关闭抛 "document closed"。已删除并加注释。测试侧同时把手工拼字节的假 PDF 换成 PyMuPDF 生成的合法 PDF。

## 验证结果

- pytest：**17 passed**（语法编译 COMPILE_OK）
- 真实服务冒烟：建知识库 → admin 上传 spec.txt → status=parsed / parse_status=done → 列表正常 → 伪装 PDF 返回 422 ✅

## 已知待办（M2 后续子步骤）

- 扫描件 OCR（PaddleOCR 中/英/西/阿）+ OCR 质量页数据
- 切分策略（chunk 表）、bge-m3 嵌入、Milvus 双索引写入
- Celery + Redis 异步流水线（当前为同步解析，小文件可用）
- 病毒扫描 ClamAV、上传审计 event 表（M2 末/M4）
- 文档列表未返回 parse_status/page_count（详情接口有），前端步骤统一

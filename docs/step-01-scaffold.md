# 步骤 1 实施记录：项目目录与工程骨架（M1）

日期：2026-11-09　对应里程碑：M1 基础框架　对应 PRD：3.2 技术选型 / 12 里程碑 / 17.4 反锁定规则

## 本步做了什么

1. 创建项目根目录，规划 `backend`（FastAPI 应用）、`docs`（实施记录）、`deploy`（部署脚本）三大部分。
2. `README.md`：项目简介、技术栈、M1–M6 实施路线、快速启动说明。
3. `backend/app/config.py`：pydantic-settings 集中配置管理，覆盖 PRD 全部配置面（数据库、Redis、Milvus、LLM 双供应商、本地模型路径、上传限制）。`.lru_cache` 保证单例。
4. `backend/app/main.py`：FastAPI 最小骨架，含 `/health` 健康检查与 CORS 中间件，预留路由挂载点。
5. `backend/requirements.txt`：依赖分里程碑注释分组，本步仅启用 fastapi/uvicorn/pydantic。
6. `backend/.env.example`：环境变量模板（密钥占位，`.gitignore` 排除真实 `.env`）。
7. `docker-compose.yml`：Milvus Standalone（etcd+minio+milvus）+ Redis 基础设施，api 服务注释待启用。
8. `app/api|core|db|models|schemas|services` 六个业务包骨架 + `tests/`。

## 与 PRD 的对应关系

- PRD 3.2 技术选型：Python/FastAPI/Milvus/SQLite/Redis/DeepSeek+Qwen 全部在配置中预留。
- PRD 17.4 反锁定规则 2：Milvus collection 命名 `chunks_bge-m3_1024` 带模型_维度后缀。
- PRD 7.2 上传校验：MAX_UPLOAD_MB=100 白名单限制先落配置。

## 自查结果

- [x] 目录结构完整，`uvicorn app.main:app` 可启动
- [x] `/health` 返回 200
- [x] `.env` 不入库；密钥未硬编码
- [ ] Docker 基础设施实际启动（本机无 Docker 时跳过，M2 前补验）

## 已知待办（转入后续步骤）

- 步骤 2：数据库模型 + JWT 登录 + RBAC（PRD 5.6 / 6.1）
- M2 前：Docker 环境验证 Milvus/Redis 可用

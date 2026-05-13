# anticheatbot

Telegram 反垃圾机器人：**Mini App 入群验证**（群规 / 问卷 + 可选 Turnstile）、**管理后台 Mini App**、**前 N 条消息 LLM 审查**；支持 **SQLite（本地）** 与 **PostgreSQL（Compose）**；**uv** 管理依赖。

## 功能概要

- 新成员入群：`JOIN_TRANSITION` 与 `new_chat_members` 双通道触发；可选禁言并发验证链接；超时未验证可踢出。
- 验证页：`/verify/index.html?t=...`，调用 `/api/verify/session`、规则提交或问卷提交。
- 管理页：`/admin/index.html`，需群管理员 `initData` + `getChatMember` 校验；**全局管理员**（见下文）不受群管理员身份限制。
- LLM：OpenAI 兼容 `chat/completions` + `response_format: json_object`（翻译、审查）。

## 本地开发（uv + SQLite）

```bash
cp .env.example .env
# 必填：BOT_TOKEN、WEBAPP_PUBLIC_URL（本地调试可用 https 隧道域名，与 Telegram 打开地址一致）
mkdir -p data
uv sync --group dev
DATABASE_URL=sqlite+aiosqlite:///./data/bot.db uv run alembic upgrade head
uv run python main.py
```

机器人需具备：**封禁/限制成员**、**删除消息**（审查删除时）等权限。

## Docker Compose（PostgreSQL）

`docker-compose.yml` 中 `bot.environment.DATABASE_URL` 会**覆盖** `.env` 里的 `DATABASE_URL`，使容器连接 `postgres` 服务。

```bash
cp .env.example .env
# 填写 BOT_TOKEN、WEBAPP_PUBLIC_URL；OPENAI 与 Turnstile 按需
docker compose up --build
```

镜像启动时执行：`alembic upgrade head` 再执行 `uv run python main.py`。对外暴露 `HTTP_PORT`（默认 `8080`），请在反向代理上配置 **HTTPS**（Telegram Web App 要求）。

`docker-compose` 已为 `bot` 设置 `WEBAPP_ROOT=/app/webapp`，保证静态资源路径正确。`Dockerfile` 中同样默认 `ENV WEBAPP_ROOT=/app/webapp`，单独 `docker run` 时亦生效。若你自行用镜像在非 `/app` 工作目录或非常规方式启动，请把 `WEBAPP_ROOT` 设为包含 `webapp/shared/` 的目录（见 `.env.example`）。

## 常用命令

- `uv run python main.py`：启动 bot 与 aiohttp。
- `DATABASE_URL=... uv run alembic upgrade head`：执行数据库迁移。
- `uv run pytest`：运行单元测试（需 `uv sync --group dev`）。

## 全局管理员

在 `.env` 中设置 `GLOBAL_ADMIN_USER_IDS`（或兼容项 `ADMIN_USER_IDS`），逗号分隔 Telegram 用户数字 ID。合并后的集合拥有：

- 管理后台：列出机器人所在全部群组、读写任意群 `group_settings` / 题库 / 统计 / 清翻译缓存（无需在该群担任管理员）；
- 入群：跳过验证与禁言；
- 发言：不参与 LLM 自动审查。

## 隐私说明

群规/问卷送翻、群消息送审会调用第三方 LLM；`initData` 仅在服务端校验，请勿记录完整 initData 到日志。

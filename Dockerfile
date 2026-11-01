FROM ghcr.nju.edu.cn/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV WEBAPP_ROOT=/app/webapp
ENV PYTHONUNBUFFERED=1
ENV UV_COMPILE_BYTECODE=1

COPY pyproject.toml uv.lock README.md main.py ./
RUN mkdir -p /etc/uv && echo '\n[[index]]\nurl = "https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple/"\ndefault = true\n' > /etc/uv/uv.toml
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY alembic ./alembic
COPY src ./src
COPY webapp ./webapp

RUN uv sync --frozen --no-dev

EXPOSE 8080

CMD ["sh", "-c", ".venv/bin/alembic upgrade head && exec .venv/bin/python main.py"]

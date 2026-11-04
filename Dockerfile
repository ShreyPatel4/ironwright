# One image for every hosted process:
#   python -m ironwright.hosted    the platform API and console
#   ironwright worker              the job worker (arrives with T4)
# Settings and secrets come from the environment at run time, never from here.

FROM node:22-slim AS console
WORKDIR /console
COPY console/package.json console/package-lock.json ./
RUN npm ci --no-fund --no-audit
COPY console/ ./
RUN npm run build

FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project
COPY ironwright/ ironwright/
# Editable on purpose: the app finds console/dist relative to its own source,
# so the source has to stay at /app and not move into site-packages.
RUN uv sync --frozen
# The Postgres driver is a dev-only extra in pyproject today. Pinned to the lock.
RUN uv pip install --python .venv/bin/python "psycopg[binary]==3.3.6"

FROM python:3.12-slim
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin ironwright \
    && mkdir /data && chown ironwright:ironwright /data
COPY --from=build /app /app
COPY --from=console /console/dist /app/console/dist
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 IRONWRIGHT_DATA=/data
# The working directory is writable, since `ironwright run` exports relative to it.
WORKDIR /data
USER ironwright
EXPOSE 8000
CMD ["python", "-m", "ironwright.hosted"]

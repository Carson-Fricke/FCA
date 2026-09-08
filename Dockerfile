FROM ghcr.io/astral-sh/uv:0.12.6 AS uv

FROM python:3.12-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build

COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY src/ ./src/
# The strategy packages live at the repository root, outside src/: the
# packaged wheel must include them or deployed backtests cannot import
# configured strategy implementations (e.g. strategies.harness_examples).
COPY strategies/ ./strategies/
RUN uv sync --locked --no-dev --no-editable
RUN test -x /opt/venv/bin/lake \
    && test -x /opt/venv/bin/harness \
    && test -x /opt/venv/bin/api \
    && test -x /opt/venv/bin/backtest-job \
    && test -x /opt/venv/bin/trader

FROM debian:bookworm-slim AS native

ARG ZIG_VERSION=0.16.0
ARG ZIG_SHA256=70e49664a74374b48b51e6f3fdfbf437f6395d42509050588bd49abe52ba3d00

ENV PATH="/opt/zig:$PATH"

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl xz-utils \
    && rm -rf /var/lib/apt/lists/

RUN curl -fsSLo /tmp/zig.tar.xz "https://ziglang.org/download/${ZIG_VERSION}/zig-x86_64-linux-${ZIG_VERSION}.tar.xz" \
    && echo "${ZIG_SHA256}  /tmp/zig.tar.xz" | sha256sum -c - \
    && mkdir /opt/zig \
    && tar -xJf /tmp/zig.tar.xz --strip-components=1 -C /opt/zig \
    && rm /tmp/zig.tar.xz

COPY build.zig build.zig.zon ./
COPY src/harness/native/ ./src/harness/native/

RUN zig build test && zig build --prefix /out

FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    LAKE_CONFIG_ROOT=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 lake \
    && useradd --uid 10001 --gid lake --create-home --shell /usr/sbin/nologin lake

COPY --from=builder /opt/venv /opt/venv
COPY --from=native /out/ /opt/native/
COPY config/ /app/config/

WORKDIR /app
USER lake

ENTRYPOINT ["lake"]
CMD ["--help"]

FROM ghcr.io/astral-sh/uv:0.12.6 AS uv

FROM rust:bookworm AS rust

# The Rust workspace is the staged port of the quant-harness native core. This
# stage compiles and tests it, then produces the loadable shared library
# (harness-native) that the runtime image carries under /opt/rust. The Python
# builder stage reuses the same toolchain to compile the PyO3 extension.
WORKDIR /build/rust

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY rust/ ./rust/

RUN cargo test --locked --workspace \
    && cargo build --locked --release -p harness-native

FROM python:3.12-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# `harness._rust` is a PyO3 extension compiled by setuptools-rust while the
# project installs, so the builder needs the Rust toolchain, a C linker, and
# the project's `setup.py`/`rust/` sources. The toolchain is copied from the
# Rust stage to keep one pinned version across the image.
COPY --from=rust /usr/local/cargo /usr/local/cargo
COPY --from=rust /usr/local/rustup /usr/local/rustup
ENV RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    PATH="/usr/local/cargo/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock setup.py ./
RUN uv sync --locked --no-dev --no-install-project

COPY src/ ./src/
COPY rust/ ./rust/
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

FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    LAKE_CONFIG_ROOT=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 lake \
    && useradd --uid 10001 --gid lake --create-home --shell /usr/sbin/nologin lake

COPY --from=builder /opt/venv /opt/venv
# The Rust native core shared library consumed by the ctypes loader.
COPY --from=rust /build/rust/target/release/libharness_native.so /opt/rust/libharness_native.so
COPY config/ /app/config/

WORKDIR /app
# The CLI artifact convention writes under .local/ (backtest artifacts,
# local trader state); the runtime user needs a writable mount point there.
RUN mkdir -p /app/.local && chown lake:lake /app/.local
USER lake

ENTRYPOINT ["lake"]
CMD ["--help"]

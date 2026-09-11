FROM ghcr.io/astral-sh/uv:0.12.6 AS uv

FROM python:3.12-slim AS rust

# The process binaries embed CPython through PyO3 to drive user-authored Python
# strategies, so they must be compiled against the SAME interpreter and glibc
# the runtime ships. Base this stage on the runtime image and install Rust into
# the canonical /usr/local paths the builder stage copies the toolchain from.
ENV RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    PATH="/usr/local/cargo/bin:$PATH" \
    PYO3_PYTHON=/usr/local/bin/python3.12

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl build-essential pkg-config \
    && rm -rf /var/lib/apt/lists/* \
    && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
       | sh -s -- -y --default-toolchain stable --profile minimal --no-modify-path

# The Rust workspace is the quant-harness engine and its process interfaces.
# This stage compiles the loadable shared library (native) the runtime image
# carries under /opt/rust and the harness entrypoint binaries the Python
# console-script shims exec. Tests run in CI; this stage only builds artifacts.
WORKDIR /build/src/harness

COPY src/harness/ ./

RUN cargo build --locked --release -p native \
    && cargo build --locked --release -p api --bins \
    && cargo build --locked --release -p live --bin trader

FROM python:3.12-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# `harness._rust` is a PyO3 extension compiled by setuptools-rust while the
# project installs, so the builder needs the Rust toolchain, a C linker, and
# the project's `setup.py` and `src/harness/` sources. The toolchain is copied from the
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
    PYTHONPATH="/opt/venv/lib/python3.12/site-packages" \
    LAKE_CONFIG_ROOT=/app \
    HARNESS_RUST_BINARY_DIR=/opt/rust/bin \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 lake \
    && useradd --uid 10001 --gid lake --create-home --shell /usr/sbin/nologin lake

COPY --from=builder /opt/venv /opt/venv
# The Rust native core shared library consumed by the ctypes loader.
COPY --from=rust /build/src/harness/target/release/libnative.so /opt/rust/libnative.so
# The Rust harness process binaries. The Python console scripts are thin shims
# that exec these binaries; discovery uses HARNESS_RUST_BINARY_DIR.
COPY --from=rust \
    /build/src/harness/target/release/harness \
    /build/src/harness/target/release/api \
    /build/src/harness/target/release/backtest-job \
    /build/src/harness/target/release/mcp \
    /build/src/harness/target/release/trader \
    /opt/rust/bin/
COPY config/ /app/config/

WORKDIR /app
# The CLI artifact convention writes under .local/ (backtest artifacts,
# local trader state); the runtime user needs a writable mount point there.
RUN mkdir -p /app/.local && chown lake:lake /app/.local
USER lake

ENTRYPOINT ["lake"]
CMD ["--help"]

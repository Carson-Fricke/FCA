FROM ghcr.io/astral-sh/uv:0.12.6 AS uv

FROM python:3.12-slim AS rust-base

# The lake's retained Python surface still ships in the runtime image, and the
# Rust stages share its base so the compiled binaries match the runtime glibc.
# Install Rust into the canonical /usr/local paths the builder stage copies
# the toolchain from.
ENV RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    PATH="/usr/local/cargo/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl build-essential pkg-config libssl-dev \
    && rm -rf /var/lib/apt/lists/* \
    && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
       | sh -s -- -y --default-toolchain stable --profile minimal --no-modify-path \
    && cargo install cargo-chef --locked --version 0.1.78

# The Rust workspace is the quant-harness engine and its process interfaces.
# This stage compiles the harness process binaries the runtime image serves
# directly. Tests run in CI; this stage only builds artifacts.
#
# `cargo-chef` splits dependency compilation (a stable layer keyed only on the
# workspace manifests) from the local-crate compilation (keyed on the Rust
# sources), so a source-only change reuses the dependency layer instead of
# rebuilding the full workspace. The exported BuildKit cache preserves the
# dependency layer across runs; see publish-image.yml.
FROM rust-base AS rust-planner

WORKDIR /build/src/harness
COPY src/harness/ ./
# `marketdata` path-depends on `../../lake/contracts`, whose manifest inherits
# the lake workspace root. Both manifests must exist before `cargo metadata`.
COPY src/lake/Cargo.toml /build/src/lake/Cargo.toml
COPY src/lake/contracts/ /build/src/lake/contracts/
RUN cargo chef prepare --recipe-path /build/recipe.json

FROM rust-base AS rust

WORKDIR /build/src/harness
COPY --from=rust-planner /build/recipe.json /build/recipe.json
COPY src/lake/Cargo.toml /build/src/lake/Cargo.toml
COPY src/lake/contracts/ /build/src/lake/contracts/
RUN cargo chef cook --locked --release --recipe-path /build/recipe.json \
      -p api -p live
COPY src/harness/ ./
# Cook replaces the path dependency with a recipe skeleton, so the real lake
# contracts sources are copied again before the workspace compiles.
COPY src/lake/contracts/ /build/src/lake/contracts/
# The `mcp` crate embeds config/harness/backtest.yaml at compile time via
# `include_str!(concat!(env!("CARGO_MANIFEST_DIR"), "/../../../config/..."))`.
# With WORKDIR /build/src/harness that resolves to /build/config/harness/, so
# the file must exist here before the workspace compiles.
COPY config/harness/ /build/config/harness/
RUN cargo build --locked --release -p api --bins \
    && cargo build --locked --release -p live --bin trader

# The market-lake workspace is the lake service. It shares the Rust base image
# and toolchain with the harness stages; cargo-chef caching mirrors them.
FROM rust-base AS lake-planner

WORKDIR /build/src/lake
COPY src/lake/ ./
RUN cargo chef prepare --recipe-path /build/recipe.json

FROM rust-base AS lake

WORKDIR /build/src/lake
COPY --from=lake-planner /build/recipe.json /build/recipe.json
RUN cargo chef cook --locked --release --recipe-path /build/recipe.json -p lake-cli
COPY src/lake/ ./
RUN cargo build --locked --release -p lake-cli --bins

FROM python:3.12-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build

COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY src/ ./src/
RUN uv sync --locked --no-dev --no-editable
RUN test -x /opt/venv/bin/lake \
    && test -x /opt/venv/bin/thesis

FROM python:3.12-slim AS runtime

ENV PATH="/opt/rust/bin:/usr/local/cargo/bin:/opt/venv/bin:$PATH" \
    RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    PYTHONPATH="/opt/venv/lib/python3.12/site-packages" \
    LAKE_CONFIG_ROOT=/app \
    LAKE_RUST_BINARY_DIR=/opt/rust/bin \
    HARNESS_SDK_DIR=/opt/rust/sdk/strategy-sdk \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 lake \
    && useradd --uid 10001 --gid lake --create-home --shell /usr/sbin/nologin lake

COPY --from=builder /opt/venv /opt/venv
# The harness JIT seam compiles authored programs at run time, so the runtime
# image carries the pinned Rust toolchain and the authoring SDK crate sources
# (with `num` as its sibling path dependency).
COPY --from=rust /usr/local/cargo /usr/local/cargo
COPY --from=rust /usr/local/rustup /usr/local/rustup
COPY src/harness/strategy-sdk /opt/rust/sdk/strategy-sdk
COPY src/harness/num /opt/rust/sdk/num
# The Rust harness process binaries, served directly on PATH (`harness`, `api`,
# `backtest-job`, `mcp`, `trader`).
COPY --from=rust \
    /build/src/harness/target/release/harness \
    /build/src/harness/target/release/api \
    /build/src/harness/target/release/backtest-job \
    /build/src/harness/target/release/mcp \
    /build/src/harness/target/release/trader \
    /opt/rust/bin/
# The Rust market-lake binary. The `lake` console script is a thin shim that
# execs it; discovery uses LAKE_RUST_BINARY_DIR.
COPY --from=lake /build/src/lake/target/release/lake /opt/rust/bin/lake
COPY config/ /app/config/

WORKDIR /app
# The CLI artifact convention writes under .local/ (backtest artifacts,
# local trader state); the runtime user needs a writable mount point there.
RUN mkdir -p /app/.local && chown lake:lake /app/.local
USER lake

ENTRYPOINT ["lake"]
CMD ["--help"]

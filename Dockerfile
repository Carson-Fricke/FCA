FROM ghcr.io/astral-sh/uv:0.12.6 AS uv

FROM python:3.12-slim-trixie AS rust-base

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
       | sh -s -- -y --default-toolchain 1.98.1 --profile minimal --no-modify-path \
    && cargo install cargo-chef --locked --version 0.1.78 --jobs 2

# Changing the build budget must not reinstall the pinned toolchain. Independent
# product stages still share workstation memory while compiling Rust and DuckDB.
ARG RUST_BUILD_JOBS=2
ENV CARGO_BUILD_JOBS=${RUST_BUILD_JOBS}

# Both workspaces refer to local crates in the other one. Every cook recipe
# needs both sets of manifests skeletonized, with its own lockfile authoritative.
# Recipe output depends on manifests/targets, not implementation source contents.
FROM rust-base AS rust-planner

WORKDIR /build/src/harness
COPY src/harness/ ./
# `marketdata` path-depends on `../../lake/contracts`, whose manifest inherits
# the lake workspace root. Both manifests must exist before `cargo metadata`.
COPY src/lake/ /build/src/lake/
RUN cargo chef prepare --recipe-path /build/harness-recipe.json \
    && cd /build/src/lake && cargo chef prepare --recipe-path /build/lake-recipe.json
COPY scripts/ci/merge_cargo_recipes.py /build/merge_cargo_recipes.py
RUN python /build/merge_cargo_recipes.py /build/harness-recipe.json /build/lake-recipe.json /build/harness-cook.json --workspace harness \
    && python /build/merge_cargo_recipes.py /build/harness-recipe.json /build/lake-recipe.json /build/lake-cook.json --workspace lake

# Compile the worker/cleanup dependencies once. The two build branches inherit
# immutable artifacts from this layer; they never share a mutable target mount.
# The harness branch can reuse compatible dependencies, while the finite worker
# build never compiles the harness or bundled DuckDB.
FROM rust-base AS lake-deps
ENV CARGO_TARGET_DIR=/build/target
WORKDIR /build/src/lake
COPY --from=rust-planner /build/lake-cook.json /build/lake-cook.json
RUN cargo chef cook --locked --release --recipe-path /build/lake-cook.json \
      -p lake-cli -p lake-ingest-worker -p lake-ingest-jobs --bins

FROM lake-deps AS harness-deps
WORKDIR /build/src/harness
COPY --from=rust-planner /build/harness-cook.json /build/harness-cook.json
RUN cargo chef cook --locked --release --recipe-path /build/harness-cook.json \
      -p api -p live --bins

FROM harness-deps AS rust
COPY src/harness/ ./
# Cook replaces the path dependency with a recipe skeleton, so the real lake
# contracts sources are copied again before the workspace compiles.
COPY src/lake/ /build/src/lake/
# The `mcp` crate embeds config/harness/backtest.yaml at compile time via
# `include_str!(concat!(env!("CARGO_MANIFEST_DIR"), "/../../../config/..."))`.
# With WORKDIR /build/src/harness that resolves to /build/config/harness/, so
# the file must exist here before the workspace compiles.
COPY config/harness/ /build/config/harness/
RUN cargo build --locked --release -p api -p live --bins

# Cleanup and finite provider execution share the lake dependency graph and
# optimized artifacts. The images copy only the binaries they actually serve.
FROM lake-deps AS ingestion-builder
WORKDIR /build/src/lake
COPY src/lake/ ./
COPY src/harness/ /build/src/harness/
RUN cargo build --locked --release -p lake-cli -p lake-ingest-worker -p lake-ingest-jobs --bins

# Match the builders' Debian/glibc release without an unused Python runtime.
FROM debian:trixie-slim AS ingestion-runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libssl3t64 libgcc-s1 zlib1g libzstd1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ingestion-builder /build/target/release/lake-ingest-worker /usr/local/bin/lake-ingest-worker
COPY --from=ingestion-builder /build/target/release/lake-ingest-dispatcher /usr/local/bin/lake-ingest-dispatcher
COPY config/market-lake/ /app/config/market-lake/
ENV LAKE_CONFIG_ROOT=/app
WORKDIR /app
RUN mkdir /app/.local && chown 10001:10001 /app/.local
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/lake-ingest-worker"]

FROM python:3.12-slim-trixie AS python-sources
WORKDIR /build
COPY pyproject.toml ./
COPY scripts/ci/prepare_python_sources.py /tmp/prepare_python_sources.py
COPY src/ ./src/
RUN python /tmp/prepare_python_sources.py /build /wheel-inputs

FROM python:3.12-slim-trixie AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build

COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY --from=python-sources /wheel-inputs/src/ ./src/
RUN uv sync --locked --no-dev --no-editable
COPY scripts/ci/installed_sdk_smoke.py /tmp/installed_sdk_smoke.py
RUN uv run --no-sync python -I /tmp/installed_sdk_smoke.py
RUN test -x /opt/venv/bin/lake \
    && test -x /opt/venv/bin/thesis

FROM python:3.12-slim-trixie AS runtime

# The JIT seam compiles authored Rust programs at run time, so the runtime
# image carries the pinned toolchain and needs a C toolchain for crate build
# scripts (the linker `cc`).
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/opt/rust/bin:/usr/local/cargo/bin:/opt/venv/bin:$PATH" \
    RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    PYTHONPATH="/opt/venv/lib/python3.12/site-packages" \
    LAKE_CONFIG_ROOT=/app \
    LAKE_RUST_BINARY_DIR=/opt/rust/bin \
    LAKE_LOCAL_DATA_DIR=/app/.local/market-lake \
    LAKE_LOCAL_WORKER_EXECUTABLE=/usr/local/bin/lake-ingest-worker \
    HARNESS_SDK_DIR=/opt/rust/sdk/strategy-sdk \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 lake \
    && useradd --uid 10001 --gid lake --create-home --shell /usr/sbin/nologin lake

COPY --link --from=builder /opt/venv /opt/venv
# The harness JIT seam compiles authored programs at run time, so the runtime
# image carries the pinned Rust toolchain and the authoring SDK crate sources
# (with `num` as its sibling path dependency).
# The JIT seam compiles authored programs at run time under the unprivileged
# runtime user, so that user must be able to write the cargo home copied from
# the dependency stage (registry downloads and cache locks). Copy the immutable
# dependency and toolchain snapshots, rather than mutable source-build caches.
# Linked payload layers can be reused independently of earlier runtime copies.
COPY --link --chown=10001:10001 --from=harness-deps /usr/local/cargo /usr/local/cargo
# The source build resolves every component requested by the workspace pin.
# rust-base has only the minimal profile; copying it would make rootless JIT
# probes try to install the pin's missing clippy/rustfmt components at runtime.
COPY --link --from=rust /usr/local/rustup /usr/local/rustup
COPY --link src/harness/rust-toolchain.toml /opt/rust/runtime-probe/rust-toolchain.toml
COPY --link src/harness/strategy-sdk /opt/rust/sdk/strategy-sdk
COPY --link src/harness/num /opt/rust/sdk/num
COPY --link src/harness/contracts /opt/rust/sdk/contracts
COPY --link src/lake/contracts /opt/rust/lake/contracts
# The Rust harness process binaries, served directly on PATH (`harness`, `api`,
# `backtest-job`, `screen-job`, `mcp`, `trader`).
COPY --link --from=rust \
    /build/target/release/harness \
    /build/target/release/api \
    /build/target/release/backtest-job \
    /build/target/release/harness-worker \
    /build/target/release/screen-job \
    /build/target/release/market-data-profile \
    /build/target/release/mcp \
    /build/target/release/trader \
    /opt/rust/bin/
# The Rust market-lake binary. The `lake` console script is a thin shim that
# execs it; discovery uses LAKE_RUST_BINARY_DIR.
COPY --link --from=ingestion-builder /build/target/release/lake /opt/rust/bin/lake
COPY --link --from=ingestion-builder /build/target/release/lake-ingest-worker /usr/local/bin/lake-ingest-worker
COPY --link --from=ingestion-builder /build/target/release/lake-ingest-dispatcher /usr/local/bin/lake-ingest-dispatcher
COPY config/ /app/config/
RUN cargo metadata --offline --format-version 1 --no-deps \
    --manifest-path /opt/rust/sdk/strategy-sdk/Cargo.toml > /dev/null

WORKDIR /app
# The CLI artifact convention writes under .local/ (backtest artifacts,
# local trader state); the runtime user needs a writable mount point there.
RUN mkdir -p /app/.local && chown lake:lake /app/.local
USER lake

# Exercise the actual JIT pin with the read-only toolchain and runtime UID.
# No component download or toolchain mutation is permitted in production.
RUN --network=none cd /opt/rust/runtime-probe \
    && rustc --version --verbose && cargo --version --verbose \
    && rustup which rustc && rustup which cargo

ENTRYPOINT ["lake"]
CMD ["--help"]

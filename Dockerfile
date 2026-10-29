# syntax=docker/dockerfile:1.7
# tvsign image: plumesign (built from vendor/Impactor) + FastAPI backend + web UI.
#
#   scripts/vendor-impactor.sh            # once, and whenever the Impactor fork changes
#   docker build -t tvsign .
#   docker build --build-arg PLUME_LIGHT=1 -t tvsign .   # low-memory hosts (Raspberry Pi 4 GB)
#   docker buildx build --target plumesign --output type=local,dest=out .   # binary only
ARG RUST_VERSION=1.98
ARG PYTHON_VERSION=3.13
ARG DEBIAN=trixie

# ---------- 1. plumesign: cargo-chef caches the dependency build in its own layer ----------
FROM rust:${RUST_VERSION}-slim-${DEBIAN} AS chef
ARG CARGO_CHEF_VERSION=0.1.71
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential cmake pkg-config git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN cargo install --locked cargo-chef --version "${CARGO_CHEF_VERSION}"
WORKDIR /src

FROM chef AS planner
COPY vendor/Impactor .
RUN cargo chef prepare --recipe-path recipe.json

FROM chef AS rust-build
# Release profile without debug info (upstream keeps full debuginfo + split files, useless here).
ENV CARGO_PROFILE_RELEASE_DEBUG=0 \
    CARGO_PROFILE_RELEASE_SPLIT_DEBUGINFO=off \
    CARGO_PROFILE_RELEASE_INCREMENTAL=false \
    CARGO_PROFILE_RELEASE_STRIP=symbols \
    CARGO_TERM_COLOR=never
# PLUME_LIGHT=1: no LTO, 16 codegen units, 2 jobs. Slightly bigger binary, but builds on a 4 GB Pi.
ARG PLUME_LIGHT=0
COPY --from=planner /src/recipe.json recipe.json
RUN if [ "$PLUME_LIGHT" = 1 ]; then export CARGO_PROFILE_RELEASE_LTO=false \
        CARGO_PROFILE_RELEASE_CODEGEN_UNITS=16 CARGO_BUILD_JOBS=2; fi; \
    cargo chef cook --release --locked --recipe-path recipe.json -p plumesign
COPY vendor/Impactor .
RUN if [ "$PLUME_LIGHT" = 1 ]; then export CARGO_PROFILE_RELEASE_LTO=false \
        CARGO_PROFILE_RELEASE_CODEGEN_UNITS=16 CARGO_BUILD_JOBS=2; fi; \
    cargo build --release --locked -p plumesign \
    && mkdir -p /out && install -m 755 target/release/plumesign /out/plumesign \
    && /out/plumesign --version

# Export stage: `--target plumesign --output type=local,dest=out` writes out/plumesign.
FROM scratch AS plumesign
COPY --from=rust-build /out/plumesign /plumesign

# ---------- 2. Python wheels (hash-checked; compilers stay out of the final image) ----------
FROM python:${PYTHON_VERSION}-slim-${DEBIAN} AS py-build
COPY requirements.lock /tmp/requirements.lock
RUN --mount=type=cache,target=/root/.cache/pip \
    pip wheel --require-hashes -w /wheels -r /tmp/requirements.lock

# ---------- 3. runtime ----------
FROM python:${PYTHON_VERSION}-slim-${DEBIAN}
ARG TVSIGN_VERSION=1.0.2-dev
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini tzdata ca-certificates \
    && (command -v setpriv >/dev/null || apt-get install -y --no-install-recommends setpriv) \
    && rm -rf /var/lib/apt/lists/* \
    && setpriv --version >/dev/null \
    && groupadd -g 1000 tvsign \
    && useradd -u 1000 -g 1000 -d /data/home -M -s /usr/sbin/nologin tvsign
WORKDIR /app
COPY requirements.lock .
RUN --mount=type=bind,from=py-build,source=/wheels,target=/tmp/wheels \
    pip install --no-cache-dir --no-index --find-links /tmp/wheels --require-hashes -r requirements.lock
COPY --from=rust-build /out/plumesign /usr/local/bin/plumesign
COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/tvsign-entrypoint
COPY docker/healthcheck.py /usr/local/lib/tvsign/healthcheck.py
COPY app app
COPY web web
COPY LICENSE THIRD_PARTY_NOTICES.md /usr/share/doc/tvsign/
COPY vendor/Impactor/LICENSE /usr/share/doc/tvsign/plumesign-LICENSE
RUN python -m compileall -q app

# All state (Apple sessions, certificate keys, pairings, IPAs, database) lives in /data:
# back it up, never share it. The server runs as PUID:PGID (default 1000:1000), never as root.
ENV TVSIGN_DATA=/data HOME=/data/home \
    TVSIGN_HOST=0.0.0.0 TVSIGN_PORT=8765 TVSIGN_PLUMESIGN=/usr/local/bin/plumesign \
    TVSIGN_IMPORT_PLUME=0 TVSIGN_VERSION=${TVSIGN_VERSION} \
    PUID=1000 PGID=1000 \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
LABEL org.opencontainers.image.title="tvsign" \
      org.opencontainers.image.description="Keeps tvOS apps signed with a free Apple ID and reinstalled on your Apple TVs" \
      org.opencontainers.image.source="https://github.com/esalwey/tvsign" \
      org.opencontainers.image.version="${TVSIGN_VERSION}"
VOLUME /data
EXPOSE 8765
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=60s --timeout=6s --start-period=30s --retries=3 \
  CMD ["python", "/usr/local/lib/tvsign/healthcheck.py"]
ENTRYPOINT ["/usr/bin/tini", "--", "tvsign-entrypoint"]
CMD ["python", "-m", "app"]

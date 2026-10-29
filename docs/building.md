# Building tvsign yourself

Most people should use the published image (`ghcr.io/esalwey/tvsign:1`) or the Windows installer.
This page is for building from source.

## 1. Vendor plumesign's source

The image compiles `plumesign` (the patched Impactor fork with wireless tvOS pairing, check,
info, apps and install) from `vendor/Impactor`. Refresh it from an Impactor checkout whenever the
fork changes, then commit the result:

```sh
scripts/vendor-impactor.sh  [path/to/Impactor] [ref]     # Linux / macOS
scripts\vendor-impactor.ps1 [-ImpactorDir path] [-Ref ref]   # Windows
```

Only committed files are copied (`git archive`), never `target/` or local edits. The source commit
is recorded in `vendor/Impactor/VENDORED.txt`.

Some changes were made in place, not in the fork (for instance the anisette servers and the Apple sign-in of 1.0.1 and 1.0.2):
[`vendor/Impactor/PATCHES.md`](../vendor/Impactor/PATCHES.md) lists them. Port them to the fork before
the next run of the script, otherwise the script overwrites them.

## 2. Docker image

```sh
docker build -t tvsign:local .
scripts/smoke-test.sh tvsign:local       # no Apple or Apple TV traffic
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

Stages: `chef` / `planner` / `rust-build` (cargo-chef keeps the dependency build in its own cached
layer, so a change to tvsign's Python only rebuilds the last layers), `py-build` (hash-checked
wheels from `requirements.lock`), then a `python:3.13-slim` runtime with tini and tzdata that runs
as `PUID:PGID` (default 1000:1000).

Only the plumesign binary: `docker buildx build --target plumesign --output type=local,dest=out .`

### Light build (Raspberry Pi and other small hosts)

The upstream release profile uses fat LTO and a single codegen unit, which needs several GB of RAM
and a long time on a Pi. Building on a 4 GB Raspberry Pi works with:

```sh
docker build --build-arg PLUME_LIGHT=1 -t tvsign:local .
# or: PLUME_LIGHT=1 docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

`PLUME_LIGHT=1` turns LTO off, uses 16 codegen units and 2 parallel jobs (the settings the old
`build-pi.sh` used). The binary is a bit larger and just as functional. Expect 20 to 40 minutes on a
Pi 4 the first time; later builds reuse the cached dependency layer. Pulling the published arm64
image avoids all of this.

## 3. Windows desktop edition

`windows/build.ps1` builds the PyInstaller app and the per-user Inno Setup installer. It expects
`plumesign.exe`; point `TVSIGN_PLUMESIGN_EXE` at it, for example after
`cargo build --release --locked -p plumesign` inside `vendor/Impactor`.

## 4. Python dependencies

`requirements.txt` lists the direct dependencies; `requirements.lock` pins everything with hashes
for the image. After editing `requirements.txt`:

```sh
uv pip compile requirements.txt --universal --python-version 3.13 --generate-hashes --no-header -o requirements.lock
```

## 5. Logic tests

No Docker, plumesign or Apple TV needed (renewal decisions, notifications, API validation, the
classification of Apple's errors, the anisette server setting):

```sh
pip install -r requirements.txt httpx
python -m unittest discover -s tests
```

The Rust side of the sign-in (anisette servers, GrandSlam answers, two-factor decisions) has unit tests
that need no network either:

```sh
cd vendor/Impactor
cargo test -p plume_core
cargo test -p omnisette -p plumesign -- --skip fetch_anisette_remote_v3 --skip fetch_anisette_ssc_async
```

The two skipped tests of `omnisette` talk to the Internet / need Android libraries. They are older than
tvsign and were already failing without them.

## 6. CI

- `.github/workflows/docker.yml`: native amd64 and arm64 runners (no QEMU), push by digest to GHCR,
  manifest merge with semver / `latest` / `edge` tags, GitHub Actions cache per architecture,
  smoke test of each architecture and of the merged tag (pulled without credentials on `v*` tags,
  so a private GHCR package fails the release), `plumesign-linux-<arch>` artifacts.
- `.github/workflows/windows.yml`: builds `plumesign.exe` and runs `windows/build.ps1` on
  `windows-latest`, uploads the installer with SHA-256 sums.

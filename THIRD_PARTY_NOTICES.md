# Third-party notices

tvsign bundles `plumesign`, built from a fork of [PlumeImpactor](https://github.com/khcrysalis/Impactor)
(MIT, Copyright 2025 Samara M) vendored in `vendor/Impactor`. The Docker image
(`/usr/share/doc/tvsign`) and the Windows installer (`licenses` folder of the installation) ship this
file, tvsign's `LICENSE` and plumesign's `LICENSE`. The licenses of plumesign's Rust dependencies
(notably [idevice](https://github.com/jkcoxson/idevice), MIT) are declared by each crate; the exact
list is `vendor/Impactor/Cargo.lock`. The source of every vendored component is in this repository.

The `plume_core` crate (`vendor/Impactor/crates/plume_core`) is licensed MPL-2.0, not MIT: its
source files stay under that license (https://www.mozilla.org/MPL/2.0/), and their source is
available in `vendor/Impactor/crates/plume_core` of this repository, including our modifications.

Python dependencies: FastAPI (MIT), Starlette (BSD-3-Clause), Uvicorn (BSD-3-Clause),
python-multipart (Apache-2.0), Pydantic (MIT). The Windows edition also bundles pywebview (BSD-3-Clause),
pystray (LGPL-3.0, used unmodified) and Pillow (MIT-CMU). pystray is packaged by PyInstaller in the
application folder (`_internal`), not as a separate installable library; its unmodified source is
available from https://github.com/moses-palmer/pystray, and building tvsign from this repository
(docs/building.md) with another version of it gives a working replacement.

tvsign does not include or redistribute NuvioTV or any other app: it downloads release IPAs at run time.

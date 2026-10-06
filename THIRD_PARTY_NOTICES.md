# Third-party notices

tvsign bundles `plumesign`, built from a fork of [PlumeImpactor](https://github.com/khcrysalis/Impactor)
(MIT, Copyright 2025 Samara M) vendored in `vendor/Impactor`. Its `LICENSE` file and the licenses of
its Rust dependencies (notably [idevice](https://github.com/jkcoxson/idevice), MIT) ship with every
binary distribution.

Python dependencies: FastAPI (MIT), Starlette (BSD-3-Clause), Uvicorn (BSD-3-Clause),
python-multipart (Apache-2.0), Pydantic (MIT). The Windows edition also bundles pywebview (BSD-3-Clause),
pystray (LGPL-3.0, used unmodified as a separate library) and Pillow (MIT-CMU).

tvsign does not include or redistribute NuvioTV or any other app: it downloads release IPAs at run time.

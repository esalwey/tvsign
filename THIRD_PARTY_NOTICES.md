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

The Windows anisette handling of `plumesign` (version 1.0.1: client info and user agent taken from the
anisette server, provisioning again on error -45061, synchronization on `hsc` 434, the `hsc` 409 case of
the two-factor step) follows [isideload](https://github.com/nab138/isideload) (MIT), re-written for
`omnisette` and `plume_core` (see `vendor/Impactor/PATCHES.md`). Its license:

> MIT License
>
> Copyright (c) 2025 nab138
>
> Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
> associated documentation files (the "Software"), to deal in the Software without restriction, including
> without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the
> following conditions:
>
> The above copyright notice and this permission notice shall be included in all copies or substantial
> portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT
> LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO
> EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
> IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE
> USE OR OTHER DEALINGS IN THE SOFTWARE.

Python dependencies: FastAPI (MIT), Starlette (BSD-3-Clause), Uvicorn (BSD-3-Clause),
python-multipart (Apache-2.0), Pydantic (MIT). The Windows edition also bundles pywebview (BSD-3-Clause),
pystray (LGPL-3.0, used unmodified) and Pillow (MIT-CMU). pystray is packaged by PyInstaller in the
application folder (`_internal`), not as a separate installable library; its unmodified source is
available from https://github.com/moses-palmer/pystray, and building tvsign from this repository
(docs/building.md) with another version of it gives a working replacement.

tvsign does not include or redistribute NuvioTV or any other app: it downloads release IPAs at run time.

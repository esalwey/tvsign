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

The anisette handling of `plumesign` (since 1.0.1: client info and user agent taken from the anisette
server, a new anisette identity on error -45061, the headers and the Mac client info of the requests to
Apple's provisioning service, synchronization on `hsc` 434, the `hsc` 409 case of the two-factor step; and,
since 1.0.2, a list of servers tried in turn with one identity per server) follows
[isideload](https://github.com/nab138/isideload) (MIT), re-written for `omnisette` and `plume_core` (see
`vendor/Impactor/PATCHES.md`). Its license:

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
python-multipart (Apache-2.0), Pydantic (MIT), cryptography (Apache-2.0 or BSD-3-Clause; its wheels carry
OpenSSL, Apache-2.0; it reads the .p12 files and seals the imported certificates). The Windows edition also bundles pywebview (BSD-3-Clause),
pystray (LGPL-3.0, used unmodified) and Pillow (MIT-CMU). pystray is packaged by PyInstaller in the
application folder (`_internal`), not as a separate installable library; its unmodified source is
available from https://github.com/moses-palmer/pystray, and building tvsign from this repository
(docs/building.md) with another version of it gives a working replacement.

Windows installer, other bundled components (all in the application folder, `_internal`, unless noted):

- Python runtime (PSF License), with the libraries it carries: OpenSSL 3 (`libcrypto-3.dll`, `libssl-3.dll`;
  Apache-2.0), SQLite (public domain), libffi (MIT), expat (MIT).
- PyInstaller bootloader (`tvsign.exe` is its output; GPL-2.0 with the PyInstaller exception that allows
  distributing the bundled application under its own license) and pywin32-ctypes (BSD-3-Clause).
- pythonnet and clr_loader (MIT), which connect pywebview to .NET; the Microsoft Edge WebView2 SDK libraries
  (`Microsoft.Web.WebView2.Core.dll`, `Microsoft.Web.WebView2.WinForms.dll`, `WebBrowserInterop.*.dll` in
  `_internal\webview\lib`; Copyright Microsoft Corporation, BSD-style license of the Microsoft.Web.WebView2
  package). The WebView2 runtime itself is not bundled: it ships with Windows 11 and recent Windows 10,
  or is installed from Microsoft.
- bottle (MIT), proxy_tools (BSD-3-Clause), six (MIT), cffi (MIT), pycparser (BSD-3-Clause), setuptools (MIT).
- Pure Python dependencies of the web server, bundled in the same way: Click (BSD-3-Clause), h11 (MIT),
  AnyIO (MIT), idna (BSD-3-Clause), typing_extensions (PSF), annotated-types (MIT), typing-inspection (MIT),
  pydantic-core (MIT).
- Microsoft Visual C++ runtime libraries (`VCRUNTIME140.dll`, `VCRUNTIME140_1.dll`; Microsoft Visual C++
  Redistributable, redistributable under Microsoft's terms), also copied next to `plumesign.exe` because
  that program imports them.
- The installer is built with Inno Setup (Jordan Russell and Martijn Laan; its license allows redistributing
  the generated setup program).

The exact Python versions are pinned, with hashes, in `requirements.lock` (image) and
`windows/requirements-lock.txt` (Windows build).

tvsign does not include or redistribute NuvioTV or any other app: it downloads release IPAs at run time.

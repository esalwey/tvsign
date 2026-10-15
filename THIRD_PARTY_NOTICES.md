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

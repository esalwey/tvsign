# License: decision pending

No license has been chosen for tvsign yet, so this repository has no `LICENSE` file. Until one is
added, the default applies: all rights reserved. Pick one before publishing anything.

## What constrains the choice

- **pymobiledevice3 (GPL-3.0) is gone.** Earlier versions of tvsign imported it, which would have
  forced GPL-3.0. tvsign now uses only `plumesign` for Apple TV operations, so a permissive license
  is possible again.
- **Python dependencies:** FastAPI (MIT), Starlette (BSD-3-Clause), Uvicorn (BSD-3-Clause),
  python-multipart (Apache-2.0), Pydantic (MIT). All permissive.
- **plumesign / Impactor** (vendored in `vendor/Impactor`, bundled in the image and the Windows
  installer) is MIT. Its `LICENSE` file is vendored with it and must ship with any binary
  distribution, together with the notices of its Rust dependencies.
- **NuvioTV** (GPL-3.0) is not part of tvsign: tvsign downloads release IPAs at run time and never
  redistributes them, so it does not constrain tvsign's license.

## Options

| License | Effect |
|---|---|
| **MIT** (recommended) | Same as Impactor; simplest for NAS app stores, Unraid/TrueNAS templates and forks. |
| Apache-2.0 | Permissive with an explicit patent grant; needs a NOTICE file. |
| GPL-3.0-only | Forks must stay open source; matches NuvioTV, adds friction for packagers. |

Once decided: add the `LICENSE` file, set `org.opencontainers.image.licenses` in the `Dockerfile`,
set the license field of the Windows installer, and delete this note.

r"""Which Visual C++ runtime DLLs does an exe import, and are they next to it?

plumesign.exe (Rust, MSVC) imports VCRUNTIME140.dll. PyInstaller keeps that DLL in _internal\, a folder it
registers for tvsign.exe only: the Windows loader looks for plumesign.exe's DLLs in plumesign.exe's own
folder and in System32, so on a PC without the Visual C++ Redistributable it would not start
(STATUS_DLL_NOT_FOUND). The build copies the DLLs it imports next to it (app-local deployment, allowed by
Microsoft's redistribution terms); this script does the same job and checks the result.

    python pe_deps.py check dist\tvsign\plumesign.exe     exit 1 when a runtime DLL is missing next to it
"""
import os
import re
import shutil
import sys
from pathlib import Path

# Visual C++ runtime libraries that are not part of Windows itself (the Universal CRT, api-ms-win-crt-*,
# is: Windows 10 and later).
RUNTIME = re.compile(r"^(vcruntime\d+(_\d+)?|msvcp\d+(_\d+)?|concrt\d+|vcomp\d+)\.dll$", re.I)


def imported_dlls(path) -> list[str]:
    """DLL names imported by a PE file, load-time and delay-load."""
    import pefile
    pe = pefile.PE(str(path), fast_load=True)
    try:
        pe.parse_data_directories(directories=[
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"]])
        entries = list(getattr(pe, "DIRECTORY_ENTRY_IMPORT", [])) + list(getattr(pe, "DIRECTORY_ENTRY_DELAY_IMPORT", []))
        return sorted({e.dll.decode("ascii", "replace") for e in entries})
    finally:
        pe.close()


def runtime_dlls(names) -> list[str]:
    return [n for n in names if RUNTIME.match(n)]


def missing_next_to(exe) -> list[str]:
    """Runtime DLLs the exe imports that are not in its own folder (case-insensitive)."""
    exe = Path(exe)
    present = {p.name.lower() for p in exe.parent.iterdir() if p.is_file()}
    return [n for n in runtime_dlls(imported_dlls(exe)) if n.lower() not in present]


def place_runtime(exe, *search_dirs) -> list[str]:
    """Copy the runtime DLLs the exe needs next to it, from the first folder that has them
    (then from System32). Returns the names copied. Raises FileNotFoundError when one cannot be found."""
    exe = Path(exe)
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    copied = []
    for name in missing_next_to(exe):
        for folder in [*map(Path, search_dirs), system32]:
            found = next((p for p in folder.glob("*.dll") if p.name.lower() == name.lower()), None) if folder.is_dir() else None
            if found:
                shutil.copy2(found, exe.parent / found.name)
                copied.append(found.name)
                break
        else:
            raise FileNotFoundError(f"{name} (imported by {exe.name}) not found in {search_dirs} or System32")
    return copied


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "check":
        exe = Path(sys.argv[2])
        names = imported_dlls(exe)
        print(f"{exe.name} imports: {', '.join(names)}")
        absent = missing_next_to(exe)
        if absent:
            print(f"ERROR: not next to {exe.name}: {', '.join(absent)}", file=sys.stderr)
            sys.exit(1)
        print("runtime DLLs next to the exe: " + (", ".join(runtime_dlls(names)) or "none needed"))
    else:
        sys.exit(__doc__)

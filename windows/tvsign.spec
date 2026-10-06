# PyInstaller spec for the tvsign Windows desktop app (onedir, windowed, no UPX).
# Build with build.ps1, or: pyinstaller --noconfirm --clean tvsign.spec
#
# Layout of dist\tvsign\:
#   tvsign.exe          launcher (window + tray + backend)
#   plumesign.exe       next to the exe: app/config.py looks for it there first
#   _internal\          Python runtime, app\presets.json, web\, icons, version.txt
import os
import shutil
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct,
                                                 StringTable, VarFileInfo, VarStruct, VSVersionInfo)

HERE = Path(SPECPATH).resolve()
ROOT = HERE.parent                         # tvsign/ (app/, web/)
ASSETS = HERE / "assets"
PLUMESIGN = Path(os.environ.get("TVSIGN_PLUMESIGN_EXE")
                 or ROOT.parent / "Impactor" / "target" / "release" / "plumesign.exe")
VERSION = (HERE / "version.txt").read_text(encoding="utf-8").strip()

if not PLUMESIGN.is_file():
    raise SystemExit(f"plumesign.exe not found: {PLUMESIGN} (set TVSIGN_PLUMESIGN_EXE)")

nums = [int(x) for x in VERSION.split("-")[0].split(".")] + [0, 0, 0]
vtuple = tuple(nums[:4])
version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=vtuple, prodvers=vtuple, mask=0x3F, flags=0x0, OS=0x40004,
                      fileType=0x1, subtype=0x0, date=(0, 0)),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("CompanyName", "esalwey"),
            StringStruct("FileDescription", "tvsign - Apple TV app signing"),
            StringStruct("FileVersion", VERSION),
            StringStruct("InternalName", "tvsign"),
            StringStruct("LegalCopyright", "esalwey"),
            StringStruct("OriginalFilename", "tvsign.exe"),
            StringStruct("ProductName", "tvsign"),
            StringStruct("ProductVersion", VERSION),
        ])]),
        VarFileInfo([VarStruct("Translation", [0x0409, 0x04B0])]),
    ],
)

datas = [
    (str(ROOT / "web"), "web"),
    (str(ROOT / "app" / "presets.json"), "app"),
    (str(ASSETS / "tray.png"), "."),
    (str(ASSETS / "tvsign.ico"), "."),
    (str(HERE / "version.txt"), "."),
]

hiddenimports = (
    collect_submodules("app")
    + collect_submodules("uvicorn")
    + ["webview.platforms.winforms", "webview.platforms.edgechromium", "pystray._win32",
       "multipart", "python_multipart"]
)

a = Analysis(
    [str(HERE / "launcher" / "tvsign_app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pydoc_data", "test", "webview.platforms.qt",
              "webview.platforms.gtk", "webview.platforms.cocoa", "webview.platforms.android",
              "PyQt5", "PyQt6", "PySide2", "PySide6", "gi"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="tvsign",
    icon=str(ASSETS / "tvsign.ico"),
    version=version_info,
    console=False,
    debug=False,
    strip=False,
    upx=False,
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="tvsign")

# plumesign.exe sits next to tvsign.exe (config.APP_DIR), outside _internal\
shutil.copy2(PLUMESIGN, Path(DISTPATH) / "tvsign" / "plumesign.exe")

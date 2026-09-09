from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_data_files


APP_NAME = "Smart Home Controller"
root = Path(SPECPATH)
window_backend = "webview.platforms.cocoa" if sys.platform == "darwin" else "webview.platforms.winforms"
datas = [
    (str(root / "static"), "static"),
    (str(root / "smart_home.sqlite3"), "seed"),
    *collect_data_files("tzdata"),
]

a = Analysis(
    [str(root / "standalone.py")],
    pathex=[str(root)],
    binaries=[],
    datas=datas,
    hiddenimports=[window_backend],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["uvloop", "httptools", "websockets"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

if sys.platform == "darwin":
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=APP_NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    collected = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=True,
        name=APP_NAME,
    )
    app = BUNDLE(
        collected,
        name=f"{APP_NAME}.app",
        icon=None,
        bundle_identifier="local.smart-home.controller",
        info_plist={
            "CFBundleDisplayName": APP_NAME,
            "NSHighResolutionCapable": True,
        },
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name=APP_NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,
        disable_windowed_traceback=False,
    )

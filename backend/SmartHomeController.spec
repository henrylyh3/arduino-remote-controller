from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_data_files


APP_NAME = "Smart Home Controller"
root = Path(SPECPATH)
windows_icon = root / "assets" / "app-icon.ico"
macos_icon = root / "assets" / "app-icon.icns"
window_backend = "webview.platforms.cocoa" if sys.platform == "darwin" else "webview.platforms.winforms"
datas = [
    (str(root / "static"), "static"),
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
        icon=str(macos_icon),
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
        icon=str(macos_icon),
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
        icon=str(windows_icon),
    )

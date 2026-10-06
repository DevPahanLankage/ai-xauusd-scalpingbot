from pathlib import Path


project = Path(SPECPATH)
frontend = project / "src" / "xauusd_bot" / "web_dist"

a = Analysis(
    [str(project / "src" / "xauusd_bot" / "desktop_entry.py")],
    pathex=[str(project / "src")],
    binaries=[],
    datas=[(str(frontend), "xauusd_bot/web_dist")],
    hiddenimports=["uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto", "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="XAUUSD-AI",
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

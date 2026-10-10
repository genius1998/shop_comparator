# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


def data_file(path, dest="."):
    return [(path, dest)] if Path(path).exists() else []


datas = []
datas += data_file("templates", "templates")
datas += data_file("static", "static")
datas += data_file("kream_overlapping_brands-2025-10-13-06-14-57-87978-00.csv")
datas += data_file("kream_search_blacklist.csv")
datas += data_file("poizon_search_blacklist.csv")
datas += data_file("k-c46vwlba.xlsx")


a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[],
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
    name="MusinsaKreamComparer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

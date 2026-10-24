# -*- mode: python ; coding: utf-8 -*-
# Build: python -m PyInstaller --noconfirm wiz-sync.spec -> dist/wiz-sync/
# One-folder build (starts faster, fewer antivirus false positives than
# one-file); packaging/wiz-sync.iss wraps it into an installer.
from PyInstaller.utils.hooks import collect_all

datas = [('packaging/wiz-sync.ico', '.')]
binaries = []
hiddenimports = []
tmp_ret = collect_all('pyaudiowpatch')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['wiz_sync.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    [],
    exclude_binaries=True,
    name='wiz-sync',
    icon='packaging/wiz-sync.ico',
    version='packaging/version_info.txt',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX-packed exes trip antivirus heuristics
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False,
               name='wiz-sync')

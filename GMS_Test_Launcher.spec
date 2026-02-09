# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['GMS_Auto_Test_GUI.py'],
    pathex=[],
    binaries=[],
    datas=[('config.json', '.'), ('run_Device_Lock.sh', '.'), ('run_GMS_Test_Auto.sh', '.'), ('run_GSI_Burn.sh', '.'), ('misc.img', '.'), ('upgrade_tool', '.'), ('scrcpy-linux-x86_64-v3.3.4.tar.gz', '.')],
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
    name='GMS_Test_Launcher',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['monitor.py'],
    pathex=[],
    binaries=[],
    # _resource_dir() resolves to _MEIPASS when frozen, so icon/ and image/
    # must be bundled: 下拉.png (QComboBox down-arrow via QSS), 删除.png
    # (provider row delete button), cpw_icon.ico (window icon).
    datas=[('icon', 'icon'), ('image', 'image')],
    hiddenimports=['psutil'],
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
    name='tokenWatch',
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
    icon=['image\\cpw_icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='tokenWatch',
)

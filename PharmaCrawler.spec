# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包配置。
#
# 用法：pyinstaller PharmaCrawler.spec
#
# 注意：本程序是纯标准库实现，所以 datas 里必须把 config.json、
# 文档和 GUI/主题模块一起带上 —— 打包后这些文件不会被自动收集。

a = Analysis(
    ['pharma_crawler.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('config.json', '.'),
        ('pharma_gui.py', '.'),
        ('m3_theme.py', '.'),
        ('docs', 'docs'),
        ('README.md', '.'),
        ('LICENSE', '.'),
    ],
    hiddenimports=['pharma_gui', 'm3_theme'],
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
    name='PharmaCrawler',
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
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='PharmaCrawler',
)

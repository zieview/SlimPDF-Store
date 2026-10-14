# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


project_root = Path(SPEC).resolve().parent
engine_root = project_root / 'vendor' / 'store-engines'
if not (engine_root / 'inventory.json').is_file():
    raise RuntimeError('Run scripts/prepare_store_engines.py before building Store edition')
news_modules = sorted(
    {
        ".".join(path.relative_to(project_root).with_suffix("").parts)
        for path in (project_root / "slimpdf").rglob("*.py")
        if "news" in path.stem.lower() or "credential_admin" in path.stem.lower()
    }
)

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('SlimPDF_logo.png', '.'),
        ('SlimPDF_icon.ico', '.'),
        ('build_info.json', '.'),
        (str(engine_root), 'engines'),
    ],
    hiddenimports=[
        'tkinterdnd2',
        'pikepdf',
        'pymupdf',
        'PIL.ImageTk',
        'slimpdf.ui.finder_window',
        'slimpdf.ui.checker_window',
        'slimpdf.core.page_organizer',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['scripts/store_edition_runtime_hook.py'],
    excludes=news_modules + ['holidays'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='SlimPDF',
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
    icon=['SlimPDF_icon.ico'],
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='SlimPDF-Store',
)

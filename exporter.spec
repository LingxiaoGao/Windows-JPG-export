# PyInstaller onedir / windowed. All photo processing stays offline.
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files

root = Path(SPECPATH)
datas = collect_data_files('tkinterdnd2')
datas += [(str(root / 'resources' / 'exiftool'), 'resources/exiftool')]
a = Analysis([str(root / 'app.py')], pathex=[str(root)], binaries=[], datas=datas,
    hiddenimports=['bundled_tests'], hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['rawpy', 'numpy', 'libraw', 'matplotlib', 'pandas', 'scipy', 'torch'],
    noarchive=False, optimize=1)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='WindowsJPGExporter',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=False, target_arch='x86_64',
    manifest=str(root / 'exporter.manifest'))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='WindowsJPGExporter')

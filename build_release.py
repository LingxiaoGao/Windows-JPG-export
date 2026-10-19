"""Maintainer/CI build orchestration, not needed by end users."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from exiftool_runtime import write_manifest

ROOT = Path(__file__).resolve().parent
EXIFTOOL_VERSION = '13.59'
EXIFTOOL_ARCHIVE_SHA256 = '577257dd22baebe77157d905792d6ed2c5916cd03aa627f40b2175db12110ac6'


def run(args, **kwargs):
    result = subprocess.run(args, check=False, **kwargs)
    if result.returncode:
        raise RuntimeError(f'Build/test command failed, exit {result.returncode}: {args[0]}')


def prepare_tool():
    destination = ROOT / 'resources' / 'exiftool'
    if (destination / 'exiftool.exe').exists():
        write_manifest(destination)
        return
    staging = ROOT / 'build' / 'vendor'
    staging.mkdir(parents=True, exist_ok=True)
    archive = staging / f'exiftool-{EXIFTOOL_VERSION}_64.zip'
    urls = [
        f'https://oliverbetz.de/cms/files/Artikel/ExifTool-for-Windows/exiftool-{EXIFTOOL_VERSION}_64.zip',
        f'https://downloads.sourceforge.net/project/exiftool/exiftool-{EXIFTOOL_VERSION}_64.zip',
        f'https://sourceforge.net/projects/exiftool/files/exiftool-{EXIFTOOL_VERSION}_64.zip/download',
    ]
    for url in urls:
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'WindowsJPGExporter-build/3.0'})
            with urllib.request.urlopen(request, timeout=60) as response:
                archive.write_bytes(response.read())
            with zipfile.ZipFile(archive) as test:
                if test.testzip() is not None:
                    raise RuntimeError('Corrupt vendor ZIP')
            if hashlib.sha256(archive.read_bytes()).hexdigest() != EXIFTOOL_ARCHIVE_SHA256:
                raise RuntimeError('ExifTool vendor checksum mismatch')
            break
        except Exception as exc:
            print(f'Vendor mirror failed: {url}: {exc}')
    else:
        raise RuntimeError('Unable to retrieve pinned ExifTool Windows package')
    unpacked = staging / 'unpacked'
    unpacked.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        for name in z.namelist():
            target = (unpacked / name).resolve()
            if not target.is_relative_to(unpacked.resolve()):
                raise RuntimeError('Unsafe vendor archive')
        z.extractall(unpacked)
    exe = next(p for p in unpacked.rglob('*') if p.is_file() and p.name.lower() in ('exiftool.exe', 'exiftool(-k).exe'))
    shutil.copytree(exe.parent, destination)
    if exe.name.lower() == 'exiftool(-k).exe':
        (destination / exe.name).rename(destination / 'exiftool.exe')
    # ExifTool/Perl licenses are already part of the original vendor distribution.
    write_manifest(destination)


def main():
    os.chdir(ROOT)
    prepare_tool()
    run([sys.executable, 'bundled_tests.py', 'build/source-tests'])
    run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', 'exporter.spec'])
    portable = ROOT / 'dist' / 'WindowsJPGExporter'
    executable = portable / 'WindowsJPGExporter.exe'
    # Hash the actual collected bytes (PyInstaller may normalize PE resources).
    write_manifest(portable / '_internal/resources/exiftool')
    run([str(executable), '--self-test', str(ROOT / 'build' / 'packaged-tests')])
    results = json.loads((ROOT / 'build/packaged-tests/test_results.json').read_text(encoding='utf-8'))
    if not results['frozen'] or not results['passed']:
        raise RuntimeError('Frozen executable acceptance failed')
    gui_report = ROOT / 'build' / 'gui-smoke.json'
    run([str(executable), '--gui-smoke', str(gui_report)])
    gui = json.loads(gui_report.read_text(encoding='utf-8'))
    if not all((gui['gui_started'], gui['drag_drop'], gui['buttons_ready'], gui['presets'] == 3, gui['scrolling_available'])):
        raise RuntimeError('GUI smoke test failed')
    # Reproduce the user's deployment: the PROGRAM itself lives under Unicode,
    # not just the input photos. No reliance on 8.3 short-name support or PATH.
    unicode_portable = ROOT / 'build' / '便携 程序 📷' / 'WindowsJPGExporter'
    shutil.copytree(portable, unicode_portable, dirs_exist_ok=True)
    unicode_exe = unicode_portable / executable.name
    isolated_env = os.environ.copy()
    isolated_env['PATH'] = os.pathsep.join((os.path.join(os.environ['SystemRoot'], 'System32'), os.environ['SystemRoot']))
    for name in ('PYTHONPATH', 'PYTHONHOME', 'TCL_LIBRARY', 'TK_LIBRARY'):
        isolated_env.pop(name, None)
    run([str(unicode_exe), '--self-test', str(ROOT / 'build/unicode-packaged-tests')], env=isolated_env)
    unicode_tests = json.loads((ROOT / 'build/unicode-packaged-tests/test_results.json').read_text(encoding='utf-8'))
    if not unicode_tests['frozen'] or not unicode_tests['passed']:
        raise RuntimeError('Unicode deployment acceptance failed')
    run([str(unicode_exe), '--gui-smoke', str(ROOT / 'build/unicode-gui-smoke.json')], env=isolated_env)
    if not json.loads((ROOT / 'build/unicode-gui-smoke.json').read_text(encoding='utf-8'))['gui_started']:
        raise RuntimeError('Unicode deployment GUI failed')
    for name in ('README_中文.md', 'AUDIT_中文.md', 'TEST_REPORT_中文.md', 'THIRD_PARTY_LICENSES.md', 'LICENSE'):
        shutil.copy2(ROOT / name, portable / name)
    shutil.copytree(ROOT / 'licenses', portable / 'licenses', dirs_exist_ok=True)
    shutil.copytree(ROOT / 'build/packaged-tests', portable / 'test-results', dirs_exist_ok=True)
    shutil.copy2(gui_report, portable / 'test-results/gui-smoke.json')
    shutil.copytree(ROOT / 'build/unicode-packaged-tests', portable / 'test-results/unicode-deployment', dirs_exist_ok=True)
    shutil.copy2(ROOT / 'build/unicode-gui-smoke.json', portable / 'test-results/unicode-gui-smoke.json')
    manifest = []
    for path in sorted(portable.rglob('*')):
        if path.is_file():
            manifest.append(f'{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(portable).as_posix()}')
    (portable / 'MANIFEST.sha256').write_text('\n'.join(manifest) + '\n', encoding='utf-8')
    release = ROOT / 'release'
    release.mkdir(exist_ok=True)
    archive = release / 'WindowsJPGExporter-v3.0.2-win-x64.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for path in portable.rglob('*'):
            if path.is_file():
                z.write(path, Path(portable.name) / path.relative_to(portable))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix('.zip.sha256').write_text(f'{digest}  {archive.name}\n', encoding='utf-8')
    print(f'Ready: {archive.name}; SHA256 {digest}')


if __name__ == '__main__':
    main()

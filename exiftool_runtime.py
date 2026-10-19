"""Keep the bundled ANSI ExifTool/Perl runtime outside Unicode launch paths.

Only vendor binaries are copied; photo paths remain UTF-8 arguments. Each process
owns its temporary directory, validates copied bytes, and removes it on exit.
"""
from __future__ import annotations

import atexit
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading

MANIFEST = 'runtime-manifest.json'
_lock = threading.Lock()
_prepared = {}
_identities = {}
_temporary = []


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_manifest(directory):
    files = {p.relative_to(directory).as_posix(): digest(p)
             for p in sorted(directory.rglob('*')) if p.is_file() and p.name != MANIFEST}
    (directory / MANIFEST).write_text(json.dumps(files, sort_keys=True, indent=2), encoding='utf-8')


def validate(directory):
    try:
        records = json.loads((directory / MANIFEST).read_text(encoding='utf-8'))
        required = ('exiftool.exe', 'exiftool_files/perl.exe',
                    'exiftool_files/exiftool.pl', 'exiftool_files/lib/Image/ExifTool.pm')
        names = {name.casefold() for name in records} if isinstance(records, dict) else set()
        if not all(name.casefold() in names for name in required):
            raise ValueError('incomplete manifest')
        if not any(Path(name).name.startswith('perl5') and name.endswith('.dll') for name in records):
            raise ValueError('Perl DLL missing from manifest')
        for name, expected in records.items():
            path = directory / name
            if not path.resolve().is_relative_to(directory.resolve()) or digest(path) != expected:
                raise ValueError('checksum mismatch: ' + name)
        return hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError('内置 ExifTool 文件缺失或损坏，请完整重新解压 ZIP（保留 _internal），'
                           '并检查安全软件隔离记录。详情：' + str(exc)) from exc


def _ascii_path(path):
    if str(path).isascii():
        return path
    if os.name == 'nt':
        buffer = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer)) and buffer.value.isascii():
            return Path(buffer.value)
    return None


def _temporary_directory():
    candidates = [Path(tempfile.gettempdir())]
    for name in ('LOCALAPPDATA', 'WINDIR', 'SystemRoot'):
        if os.environ.get(name):
            candidates.append(Path(os.environ[name]) / 'Temp')
    for candidate in dict.fromkeys(candidates):
        base = _ascii_path(candidate)
        if base is None or not base.is_dir():
            continue
        try:
            return tempfile.TemporaryDirectory(prefix='wjpg_exiftool_', dir=str(base))
        except OSError:
            continue
    raise RuntimeError('ExifTool 需要可写的英文临时目录，但本机未找到。请将程序完整解压到'
                       '可写的纯英文目录（例如 D:\\WindowsJPGExporter）后重试；照片目录可以保留中文。')


def prepare(executable):
    executable = Path(executable).absolute()
    key = str(executable)
    with _lock:
        if key in _prepared:
            return _prepared[key]
        identity = validate(executable.parent)
        usable = _ascii_path(executable)
        if os.name == 'nt' and usable is None:
            temporary = _temporary_directory()
            target = Path(temporary.name) / 'exiftool'
            try:
                shutil.copytree(executable.parent, target)
                if validate(target) != identity:
                    raise RuntimeError('ExifTool 临时副本校验失败')
            except Exception:
                temporary.cleanup()
                raise
            _temporary.append(temporary)
            usable = target / executable.name
        result = str(usable or executable)
        _prepared[key] = result
        _identities[result] = 'bundled-exiftool-sha256:' + identity
        return result


def identity(executable):
    # Temporary names must not invalidate a completed export on the next launch.
    return _identities.get(str(executable), str(executable))


def cleanup():
    for temporary in _temporary:
        try:
            temporary.cleanup()
        except OSError:
            pass


atexit.register(cleanup)

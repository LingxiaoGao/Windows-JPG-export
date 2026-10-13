#!/usr/bin/env python3
"""Export Sony ARW to small AI-ready JPEG using ExifTool, with rawpy fallback.

Windows / PowerShell, Python 3.10+; requires Pillow and rawpy (only for fallback).
Source ARW files are never modified. Outputs and resumable SQLite log are outside source.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import uuid

VERSION = "1.0"
COLS = (
    "relative_arw", "source_arw", "source_size", "source_mtime_ns", "method",
    "embedded_tag", "embedded_width", "embedded_height", "source_width",
    "source_height", "jpg_width", "jpg_height", "preview_jpg", "full_jpg",
    "color_note", "status", "error", "processed_at"
)


def arguments():
    p = argparse.ArgumentParser(description="Recursive ARW -> JPEG for Safari animal identification (NO YOLO)")
    p.add_argument("--input", required=True, help="Folder with ARW photos; recursively scanned")
    p.add_argument("--output", required=True, help="Separate output directory; must not be nested in input")
    p.add_argument("--long-edge", type=int, default=3072, help="Max output edge in pixels (default 3072)")
    p.add_argument("--quality", type=int, default=85, help="JPEG quality 1-95 (default 85)")
    p.add_argument("--exiftool", default=r"C:\Tools\ExifTool\exiftool.exe", help="Path to exiftool.exe")
    p.add_argument("--keep-full", action="store_true", help="Also save full embedded JPEG or full rawpy render")
    p.add_argument("--limit", type=int, default=0, help="Process at most N source files (0 = all)")
    a = p.parse_args()
    if not 512 <= a.long_edge <= 12000:
        p.error("--long-edge must be between 512 and 12000")
    if not 1 <= a.quality <= 95:
        p.error("--quality must be between 1 and 95")
    if a.limit < 0:
        p.error("--limit cannot be negative")
    return a


def verify_paths(inp: Path, out: Path):
    if not inp.is_dir():
        raise ValueError(f"Input folder not found: {inp}")
    if out == inp or out.is_relative_to(inp) or inp.is_relative_to(out):
        raise ValueError("Input/output must be separate, non-nested folders. Keep output OUTSIDE RAW library.")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_bytes(path: Path, payload: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.stem + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def atomic_jpeg(im, path: Path, quality: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.stem + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("wb") as f:
            im.save(f, format="JPEG", quality=quality, subsampling=0)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def get_exiftool(arg: str):
    path = Path(arg).expanduser()
    if path.is_file():
        return str(path.resolve())
    executable = shutil.which(arg) or shutil.which("exiftool")
    return executable  # None -> skip embedded and use rawpy; log reason


def valid_embedded(exiftool: str, src: Path, tag: str):
    """Return JPEG bytes, width, height for valid ExifTool binary tag; otherwise None."""
    from PIL import Image
    try:
        result = subprocess.run(
            [exiftool, "-b", f"-{tag}", str(src)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=120, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    payload = result.stdout
    try:
        with Image.open(io.BytesIO(payload)) as im:
            if im.format != "JPEG":
                return None
            im.load()  # detect corrupt/incomplete embedded images early
            return payload, im.width, im.height
    except (OSError, ValueError):
        return None


def choose_embedded(exiftool: str | None, src: Path, target: int):
    """Find highest-resolution available embedded JPEG from two common ExifTool tags."""
    if not exiftool:
        return None
    best = None
    for tag in ("JpgFromRaw", "PreviewImage"):
        candidate = valid_embedded(exiftool, src, tag)
        if candidate is None:
            continue
        payload, w, h = candidate
        if best is None or w * h > best[1] * best[2]:
            best = (payload, w, h, tag)
    return best


def open_rendered_raw(src: Path):
    from PIL import Image
    try:
        import rawpy
    except ImportError as e:
        raise RuntimeError("No high-resolution embedded JPEG and rawpy is not installed") from e
    import numpy as np
    with rawpy.imread(str(src)) as raw:
        rgb = raw.postprocess(use_camera_wb=True, output_bps=8,
                              output_color=rawpy.ColorSpace.sRGB)
    im = Image.fromarray(np.ascontiguousarray(rgb))
    del rgb
    return im


def embedded_to_rgb(payload: bytes):
    from PIL import Image, ImageOps, ImageCms
    with Image.open(io.BytesIO(payload)) as source:
        im = ImageOps.exif_transpose(source).convert("RGB")
        icc = source.info.get("icc_profile")
    if icc:
        try:
            source_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            target_profile = ImageCms.createProfile("sRGB")
            converted = ImageCms.profileToProfile(im, source_profile, target_profile, outputMode="RGB")
            im.close()
            return converted, "converted_embedded_icc_to_srgb"
        except (ImageCms.PyCMSError, OSError, ValueError):
            return im, "embedded_icc_conversion_failed_assumed_srgb"
    return im, "no_embedded_icc_assumed_srgb"


def export_one(src, rel, out, target, quality, keep_full, exiftool):
    from PIL import Image
    embedded = choose_embedded(exiftool, src, target)
    preview_rel = (Path(f"previews_{target}") / rel).with_suffix(".jpg")
    full_rel = (Path("full_resolution") / rel).with_suffix(".jpg") if keep_full else None
    embedded_tag, embw, embh = "", "", ""
    if embedded:
        payload, ew, eh, tag = embedded
        embedded_tag, embw, embh = tag, ew, eh
    else:
        payload, ew, eh = None, 0, 0

    # Never upscale a small embedded thumbnail. Decode RAW when preview is too small.
    if payload is not None and max(ew, eh) >= target:
        im, color_note = embedded_to_rgb(payload)
        method = "embedded_jpeg"
    else:
        im = open_rendered_raw(src)
        color_note = "rawpy_rendered_srgb"
        method = "rawpy_fallback"

    try:
        sw, sh = im.size
        if max(sw, sh) <= target:
            resized = im.copy()
        else:
            resized = im.copy()
            resized.thumbnail((target, target), Image.Resampling.LANCZOS)
        try:
            pw, ph = resized.size
            atomic_jpeg(resized, out / preview_rel, quality)
        finally:
            resized.close()
        if keep_full:
            if method == "embedded_jpeg":
                # Exact embedded JPEG bytes; retained EXIF orientation/ICC as supplied by camera.
                atomic_bytes(out / full_rel, payload)
            else:
                atomic_jpeg(im, out / full_rel, 95)
        return dict(
            method=method, embedded_tag=embedded_tag, embedded_width=embw,
            embedded_height=embh, source_width=sw, source_height=sh,
            jpg_width=pw, jpg_height=ph,
            preview_jpg=str(preview_rel), full_jpg=str(full_rel) if full_rel else "",
            color_note=color_note,
        )
    finally:
        im.close()


def database(path):
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE IF NOT EXISTS jobs (
        relative_arw TEXT PRIMARY KEY, source_arw TEXT, source_size INTEGER,
        source_mtime_ns INTEGER, config_hash TEXT, method TEXT, embedded_tag TEXT,
        embedded_width INTEGER, embedded_height INTEGER, source_width INTEGER,
        source_height INTEGER, jpg_width INTEGER, jpg_height INTEGER,
        preview_jpg TEXT, full_jpg TEXT, color_note TEXT, status TEXT,
        error TEXT, processed_at TEXT
    )""")
    db.commit()
    return db


def settings_hash(args):
    settings = dict(long_edge=args.long_edge, quality=args.quality,
                    keep_full=args.keep_full, version=VERSION)
    return hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()[:16]


def save_record(db, row):
    cols = ["relative_arw", "source_arw", "source_size", "source_mtime_ns", "config_hash"] + list(COLS[4:])
    sql = f"INSERT OR REPLACE INTO jobs ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})"
    db.execute(sql, [row.get(c, "") for c in cols])
    db.commit()


def write_manifest(db, out):
    tmp = out / ("manifest.csv." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(COLS)
            for values in db.execute("SELECT " + ",".join(COLS) + " FROM jobs ORDER BY relative_arw"):
                writer.writerow(values)
        os.replace(tmp, out / "manifest.csv")
    finally:
        tmp.unlink(missing_ok=True)


def main():
    a = arguments()
    inp = Path(a.input).expanduser().resolve()
    out = Path(a.output).expanduser().resolve()
    verify_paths(inp, out)
    files = sorted((p for p in inp.rglob("*") if p.is_file() and p.suffix.lower() == ".arw"),
                   key=lambda p: str(p).casefold())
    if a.limit:
        files = files[:a.limit]
    if not files:
        print("No ARW files found.")
        return 1
    out.mkdir(parents=True, exist_ok=True)
    exiftool = get_exiftool(a.exiftool)
    if not exiftool:
        print("WARNING: ExifTool not found; using full RAW decode (rawpy) for every image.")
    db = database(out / "progress.sqlite3")
    signature = settings_hash(a)
    ok = skipped = errors = 0
    try:
        for index, src in enumerate(files, 1):
            rel = src.relative_to(inp)
            reltext = rel.as_posix()
            stat = src.stat()
            current = db.execute(
                "SELECT source_size,source_mtime_ns,config_hash,status,preview_jpg,full_jpg "
                "FROM jobs WHERE relative_arw=?", (reltext,)
            ).fetchone()
            if current and (current[0] == stat.st_size and current[1] == stat.st_mtime_ns
                            and current[2] == signature and current[3] == "ok"
                            and (out / current[4]).is_file()
                            and ((not a.keep_full) or (current[5] and (out / current[5]).is_file()))):
                skipped += 1
                print(f"[{index}/{len(files)}] SKIP {reltext}", flush=True)
                continue
            row = dict(relative_arw=reltext, source_arw=str(src), source_size=stat.st_size,
                       source_mtime_ns=stat.st_mtime_ns, config_hash=signature,
                       status="error", error="", processed_at=now())
            try:
                info = export_one(src, rel, out, a.long_edge, a.quality, a.keep_full, exiftool)
                row.update(info)
                row["status"] = "ok"
                ok += 1
                print(f"[{index}/{len(files)}] OK   {reltext}  "
                      f"{info['method']} {info['source_width']}x{info['source_height']}"
                      f" -> {info['jpg_width']}x{info['jpg_height']}", flush=True)
            except Exception as exc:
                errors += 1
                row["error"] = f"{type(exc).__name__}: {exc}"
                print(f"[{index}/{len(files)}] ERROR {reltext}: {row['error']}", file=sys.stderr, flush=True)
            save_record(db, row)
    finally:
        write_manifest(db, out)
        db.close()
    print(f"\nDone. New: {ok}; reused: {skipped}; errors: {errors}; "
          f"manifest: {out / 'manifest.csv'}", flush=True)
    return 2 if errors else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted. Existing conversions are tracked in progress.sqlite3; rerun to resume.", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)

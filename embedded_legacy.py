#!/usr/bin/env python3
"""Sony ARW embedded-JPEG-only export, derived from the user's v1.0 script.

Python 3.10+, Pillow 9.1+, ExifTool. No RAW decoder is used or imported.
share: orient pixels, resize without upscaling, save sRGB JPEG (default 2560).
extract: byte-identical embedded JPEG plus an orientation JSON sidecar.
RAW inputs are only read; outputs and resumable SQLite log stay outside input.
"""
from __future__ import annotations

import argparse
import csv
import ctypes
from dataclasses import dataclass
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

VERSION = "2.0"
TAGS = ("JpgFromRaw", "PreviewImage", "OtherImage", "ThumbnailImage")
COLS = (
    "relative_arw", "source_arw", "source_size", "source_mtime_ns", "method",
    "embedded_tag", "embedded_width", "embedded_height", "source_width",
    "source_height", "jpg_width", "jpg_height", "preview_jpg", "full_jpg",
    "color_note", "status", "error", "processed_at", "raw_orientation",
    "jpeg_orientation", "applied_orientation", "orientation_note", "sidecar",
    "embedded_sha256", "warnings", "effective_orientation",
)
DBCOLS = list(COLS[:4]) + ["config_hash"] + list(COLS[4:])


def arguments(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=VERSION)
    p.add_argument("--input", required=True, help="ARW folder, recursively scanned")
    p.add_argument("--output", required=True, help="Separate, non-nested output folder")
    p.add_argument("--mode", choices=("share", "extract"), default="share")
    p.add_argument("--long-edge", type=int, default=2560,
                   help="share target long edge, default 2560; never upscale")
    p.add_argument("--min-edge", type=int, default=0,
                   help="extract minimum embedded long edge; 0 accepts any valid JPEG")
    p.add_argument("--quality", type=int, default=90, help="share JPEG quality 1-95")
    p.add_argument("--exiftool", default=r"C:\Tools\ExifTool\exiftool.exe")
    p.add_argument("--keep-full", action="store_true",
                   help="share: also save exact embedded JPEG and sidecar")
    p.add_argument("--orientation-source", choices=("auto", "raw", "jpeg", "none"),
                   default="auto", help="Override only after inspecting unusual metadata")
    p.add_argument("--assume-srgb", action="store_true",
                   help="Explicitly assume sRGB when color metadata is unknown/non-sRGB")
    p.add_argument("--limit", type=int, default=0, help="0 = all files")
    p.add_argument("--force", action="store_true", help="Re-export, ignoring resume cache")
    a = p.parse_args(argv)
    if not 512 <= a.long_edge <= 12000:
        p.error("--long-edge must be between 512 and 12000")
    if not 0 <= a.min_edge <= 12000:
        p.error("--min-edge must be between 0 and 12000")
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
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def atomic_jpeg(im, path: Path, quality: int, icc: bytes):
    from PIL import Image
    exif = Image.Exif()
    exif[274] = 1  # Pixels are already oriented; viewers must not rotate again.
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality, subsampling=0,
            exif=exif.tobytes(), icc_profile=icc)
    atomic_bytes(path, buf.getvalue())


def run_exiftool(exiftool: str, *args):
    # No shell/redirection, no write options, and no user ExifTool configuration.
    env = os.environ.copy()
    if os.name == "nt":
        for key in ("LC_ALL", "LC_CTYPE", "LANG", "PERL5LIB", "PERLLIB", "PERL5OPT", "PERL_UNICODE", "PERLIO"):
            env.pop(key, None)
    lines = list(map(str, args))
    if any("\n" in value or "\r" in value for value in lines):
        raise ValueError("ExifTool arguments/file names must not contain newlines")
    try:
        frozen_windows = os.name == "nt" and getattr(sys, "frozen", False)
        if frozen_windows:
            # PyInstaller's DLL search directory must not leak into Strawberry Perl.
            # Only the child launch is sanitized; restore before waiting for IO.
            env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep)
                                          if not p.casefold().startswith(str(sys._MEIPASS).casefold()))
            ctypes.windll.kernel32.SetDllDirectoryW(None)
        old_mode = ctypes.windll.kernel32.SetErrorMode(3) if os.name == "nt" else None
        try:
            process = subprocess.Popen(
                [exiftool, "-config", "", "-charset", "filename=UTF8", "-@", "-"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        finally:
            if frozen_windows:
                ctypes.windll.kernel32.SetDllDirectoryW(str(sys._MEIPASS))
            if old_mode is not None:
                ctypes.windll.kernel32.SetErrorMode(old_mode)
        try:
            stdout, stderr = process.communicate(("\n".join(lines) + "\n").encode("utf-8"), timeout=120)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        result = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"ExifTool failed: {exc}") from exc
    warning = result.stderr.decode("utf-8", errors="replace").strip()
    if result.returncode:
        raise RuntimeError(f"ExifTool exit {result.returncode}: {warning}")
    return result.stdout, warning


def get_exiftool(arg: str):
    path = Path(arg).expanduser()
    executable = str(path.resolve()) if path.is_file() else shutil.which(arg)
    if not executable:
        executable = shutil.which("exiftool")
    if not executable:
        raise RuntimeError("ExifTool not found. Install ExifTool or set --exiftool. RAW decoding is disabled.")
    version, _ = run_exiftool(executable, "-ver")
    return executable, version.decode("ascii").strip()


def raw_metadata(exiftool: str, src: Path):
    data, warning = run_exiftool(
        exiftool, "-j", "-n", "-G1", "-IFD0:Orientation", "-IFD0:ImageWidth",
        "-IFD0:ImageHeight", "-ExifIFD:ExifImageWidth", "-ExifIFD:ExifImageHeight",
        "-ExifIFD:ColorSpace", src,
    )
    records = json.loads(data.decode("utf-8"))
    if len(records) != 1 or "ExifTool:Error" in records[0]:
        raise RuntimeError(f"Cannot read ARW metadata: {records}")
    return records[0], warning


def orientation_value(value):
    try:
        n = int(value)
        return n if 1 <= n <= 8 else None
    except (TypeError, ValueError):
        return None


@dataclass
class Embedded:
    payload: bytes
    width: int
    height: int
    tag: str
    orientation: int | None
    color_space: int | None
    container_format: str = "JPEG"
    frames: int = 1


class EmbeddedError(RuntimeError):
    def __init__(self, message, info=None):
        super().__init__(message)
        self.info = info or {}


def choose_embedded(exiftool: str, src: Path, minimum: int):
    from PIL import Image
    candidates, notes = [], []
    for tag in TAGS:
        try:
            payload, warning = run_exiftool(exiftool, "-b", f"-{tag}", src)
            if warning:
                notes.append(f"{tag}: {warning}")
            if not payload:
                notes.append(f"{tag}: absent")
                continue
            with Image.open(io.BytesIO(payload)) as im:
                if im.format not in ("JPEG", "MPO") or not payload.startswith(b"\xff\xd8\xff"):
                    raise ValueError(f"embedded data is not JPEG (detected {im.format})")
                im.seek(0)  # Sony MPF/MPO JPEG: use the primary photo, not its preview.
                im.load()  # Validate the embedded JPEG, never the sensor RAW.
                exif = im.getexif()
                color = exif.get_ifd(34665).get(40961)
                candidates.append(Embedded(payload, im.width, im.height, tag,
                                           orientation_value(exif.get(274)), color,
                                           im.format, getattr(im, "n_frames", 1)))
        except (RuntimeError, OSError, ValueError, SyntaxError) as exc:
            notes.append(f"{tag}: {exc}")
    if not candidates:
        raise EmbeddedError("No valid embedded JPEG. RAW decoding is disabled. " + "; ".join(notes))
    eligible = [c for c in candidates if max(c.width, c.height) >= minimum]
    best = max(eligible or candidates, key=lambda c: (c.width * c.height, max(c.width, c.height)))
    if not eligible:
        sizes = ", ".join(f"{c.tag}={c.width}x{c.height}" for c in candidates)
        raise EmbeddedError(
            f"Embedded JPEG resolution insufficient: need long edge >= {minimum}px; found {sizes}. "
            "No upscaling or RAW decoding. Use --mode extract to retain the available JPEG.",
            dict(embedded_tag=best.tag, embedded_width=best.width, embedded_height=best.height,
                 warnings="; ".join(notes)),
        )
    # Absent tags are routine; retain actual tool/decode warnings only on success.
    return best, [n for n in notes if not n.endswith(": absent")]


def resolve_orientation(c: Embedded, meta: dict, source="auto"):
    raw = orientation_value(meta.get("IFD0:Orientation"))
    jpeg = c.orientation
    if source == "none":
        return 1, "explicit_no_rotation"
    if source in ("raw", "jpeg"):
        value = raw if source == "raw" else jpeg
        if value is None:
            raise RuntimeError(f"Requested {source} orientation is absent/invalid")
        return value, f"explicit_{source}"
    if jpeg not in (None, 1):
        if raw not in (None, 1, jpeg):
            raise RuntimeError(f"Conflicting orientations: ARW={raw}, JPEG={jpeg}. "
                               "Inspect the photo then set --orientation-source raw or jpeg.")
        return jpeg, "embedded_jpeg_exif"
    if raw not in (None, 1):
        # A JPEG explicitly marked normal AND already swapped relative to RAW
        # geometry can already have upright pixels. This is only a size heuristic.
        rw = meta.get("ExifIFD:ExifImageWidth") or meta.get("IFD0:ImageWidth")
        rh = meta.get("ExifIFD:ExifImageHeight") or meta.get("IFD0:ImageHeight")
        if (raw in (5, 6, 7, 8) and rw and rh
                and (int(rw) - int(rh)) * (c.width - c.height) < 0):
            if jpeg == 1:
                return 1, "jpeg_normal_already_swapped_geometry_check"
            raise RuntimeError("Ambiguous orientation: JPEG has no orientation and dimensions are already swapped. "
                               "Inspect then set --orientation-source raw or none.")
        return raw, "arw_ifd0_fallback"
    if jpeg == 1 or raw == 1:
        return 1, "explicit_normal"
    raise RuntimeError("Neither JPEG nor ARW has a valid EXIF orientation. "
                       "Inspect then use --orientation-source none if already upright.")


def embedded_to_rgb(c: Embedded, orientation: int, meta: dict, assume_srgb=False):
    from PIL import Image, ImageOps, ImageCms
    target_profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
    with Image.open(io.BytesIO(c.payload)) as source:
        icc = source.info.get("icc_profile")
        # Inject ARW fallback into the in-memory JPEG, then handle all 8 EXIF values.
        source.getexif()[274] = orientation
        with ImageOps.exif_transpose(source) as oriented:
            im = oriented.convert("RGB")
    try:
        if icc:
            profile = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            converted = ImageCms.profileToProfile(im, profile, target_profile, outputMode="RGB")
            im.close()
            im = converted
            note = "converted_embedded_icc_to_srgb"
        else:
            color = c.color_space if c.color_space is not None else meta.get("ExifIFD:ColorSpace")
            if color == 1:
                note = "exif_srgb_no_icc"
            elif assume_srgb:
                note = f"user_assumed_srgb_color_space_{color}"
            else:
                raise RuntimeError(f"No embedded ICC and color space is {color!r}, not confirmed sRGB. "
                                   "Use --mode extract, or --assume-srgb only if appropriate.")
        return im, note, target_profile.tobytes()
    except Exception:
        im.close()
        raise


def save_original(c: Embedded, src: Path, out: Path, rel: Path, meta: dict, info: dict):
    full_rel = (Path("full_resolution") / rel).with_suffix(".jpg")
    sidecar = full_rel.with_suffix(".jpg.json")
    record = dict(version=VERSION, source_arw=str(src), embedded_tag=c.tag,
                  embedded_width=c.width, embedded_height=c.height,
                  embedded_sha256=info["embedded_sha256"], raw_metadata=meta,
                  jpeg_orientation=c.orientation,
                  suggested_orientation=info.get("effective_orientation", ""),
                  orientation_note=info["orientation_note"],
                  note="JPEG is byte-identical. Sidecar is informative; image viewers do not apply it.")
    atomic_bytes(out / full_rel, c.payload)
    atomic_bytes(out / sidecar, json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8"))
    return full_rel.as_posix(), sidecar.as_posix()


def export_one(src: Path, rel: Path, out: Path, a, exiftool: str):
    meta, warning = raw_metadata(exiftool, src)
    minimum = a.long_edge if a.mode == "share" else a.min_edge
    c, notes = choose_embedded(exiftool, src, minimum)
    if warning:
        notes.append(warning)
    info = dict(method="embedded_jpeg_" + a.mode, embedded_tag=c.tag,
                embedded_width=c.width, embedded_height=c.height,
                raw_orientation=orientation_value(meta.get("IFD0:Orientation")) or "",
                jpeg_orientation=c.orientation or "", preview_jpg="", full_jpg="", sidecar="",
                embedded_sha256=hashlib.sha256(c.payload).hexdigest())
    try:
        ori, ori_note = resolve_orientation(c, meta, a.orientation_source)
    except RuntimeError as exc:
        if a.mode == "share":
            raise EmbeddedError(str(exc), info) from exc
        ori, ori_note = None, f"unresolved: {exc}"
        notes.append(ori_note)
    info.update(effective_orientation=ori or "",
                applied_orientation=(ori or "") if a.mode == "share" else "",
                orientation_note=ori_note)
    if a.mode == "extract":
        info.update(source_width=c.width, source_height=c.height,
                    jpg_width=c.width, jpg_height=c.height, color_note="unchanged_embedded_bytes")
    else:
        from PIL import Image
        try:
            im, color_note, icc = embedded_to_rgb(c, ori, meta, a.assume_srgb)
        except Exception as exc:
            raise EmbeddedError(str(exc), info) from exc
        try:
            sw, sh = im.size
            im.thumbnail((a.long_edge, a.long_edge), Image.Resampling.LANCZOS)
            preview_rel = (Path(f"previews_{a.long_edge}") / rel).with_suffix(".jpg")
            atomic_jpeg(im, out / preview_rel, a.quality, icc)
            info.update(source_width=sw, source_height=sh, jpg_width=im.width,
                        jpg_height=im.height, preview_jpg=preview_rel.as_posix(), color_note=color_note)
        finally:
            im.close()
    if a.mode == "extract" or a.keep_full:
        info["full_jpg"], info["sidecar"] = save_original(c, src, out, rel, meta, info)
    info["warnings"] = "; ".join(notes)
    return info


def database(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS jobs (relative_arw TEXT PRIMARY KEY)")
    existing = {r[1] for r in db.execute("PRAGMA table_info(jobs)")}
    integer_cols = {"source_size", "source_mtime_ns", "embedded_width", "embedded_height",
                    "source_width", "source_height", "jpg_width", "jpg_height"}
    for col in DBCOLS:
        if col not in existing:
            kind = "INTEGER" if col in integer_cols else "TEXT"
            db.execute(f"ALTER TABLE jobs ADD COLUMN {col} {kind}")
    db.commit()
    return db


def settings_hash(a, inp: Path, tool_version: str):
    keys = ("mode", "long_edge", "min_edge", "quality", "keep_full", "orientation_source", "assume_srgb")
    settings = {key: getattr(a, key) for key in keys}
    settings.update(version=VERSION, input=str(inp), exiftool_version=tool_version)
    return hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()


def save_record(db, row):
    sql = f"INSERT OR REPLACE INTO jobs ({','.join(DBCOLS)}) VALUES ({','.join('?' for _ in DBCOLS)})"
    db.execute(sql, [row.get(c, "") for c in DBCOLS])
    db.commit()


def write_manifest(db, out):
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(COLS)
    writer.writerows(db.execute("SELECT " + ",".join(COLS) + " FROM jobs ORDER BY relative_arw"))
    atomic_bytes(out / "manifest.csv", buf.getvalue().encode("utf-8-sig"))


def cached_outputs_exist(out, current, a):
    needed = [current["full_jpg"], current["sidecar"]] if a.mode == "extract" else [current["preview_jpg"]]
    if a.mode == "share" and a.keep_full:
        needed += [current["full_jpg"], current["sidecar"]]
    for name in needed:
        if not name:
            return False
        path = (out / name).resolve()
        if not path.is_relative_to(out) or not path.is_file() or path.stat().st_size == 0:
            return False
    return True


def main(argv=None):
    a = arguments(argv)
    from PIL import Image  # Fail at startup with a clear dependency error.
    if not hasattr(Image, "Resampling"):
        raise RuntimeError("Pillow >= 9.1 is required")
    inp = Path(a.input).expanduser().resolve()
    out = Path(a.output).expanduser().resolve()
    verify_paths(inp, out)
    exiftool, tool_version = get_exiftool(a.exiftool)
    files = sorted((p for p in inp.rglob("*") if p.is_file() and p.suffix.lower() == ".arw"),
                   key=lambda p: str(p).casefold())
    names = set()
    for src in files:
        name = src.relative_to(inp).with_suffix(".jpg").as_posix().casefold()
        if name in names:
            raise ValueError(f"Output name collision: {name}")
        names.add(name)
    if a.limit:
        files = files[:a.limit]
    if not files:
        print("No ARW files found.")
        return 1
    out.mkdir(parents=True, exist_ok=True)
    db = database(out / "progress.sqlite3")
    db.row_factory = sqlite3.Row
    signature = settings_hash(a, inp, tool_version)
    ok = skipped = errors = 0
    print(f"Embedded JPEG only | mode={a.mode} | ExifTool {tool_version} | RAW decoding disabled", flush=True)
    try:
        for index, src in enumerate(files, 1):
            rel = src.relative_to(inp)
            reltext = rel.as_posix()
            stat = src.stat()
            current = db.execute("SELECT * FROM jobs WHERE relative_arw=?", (reltext,)).fetchone()
            if (not a.force and current and current["source_arw"] == str(src)
                    and current["source_size"] == stat.st_size and current["source_mtime_ns"] == stat.st_mtime_ns
                    and current["config_hash"] == signature and current["status"] == "ok"
                    and cached_outputs_exist(out, current, a)):
                skipped += 1
                print(f"[{index}/{len(files)}] SKIP {reltext}", flush=True)
                continue
            row = dict(relative_arw=reltext, source_arw=str(src), source_size=stat.st_size,
                       source_mtime_ns=stat.st_mtime_ns, config_hash=signature,
                       status="error", error="", processed_at=now())
            try:
                info = export_one(src, rel, out, a, exiftool)
                row.update(info)
                row["status"] = "ok"
                ok += 1
                print(f"[{index}/{len(files)}] OK {reltext} {info['embedded_tag']} "
                      f"{info['embedded_width']}x{info['embedded_height']} -> "
                      f"{info['jpg_width']}x{info['jpg_height']} | {info['orientation_note']}", flush=True)
                if info["warnings"]:
                    print(f"  WARNING: {info['warnings']}", file=sys.stderr, flush=True)
            except Exception as exc:
                errors += 1
                row.update(getattr(exc, "info", {}))
                row["error"] = f"{type(exc).__name__}: {exc}"
                print(f"[{index}/{len(files)}] ERROR {reltext}: {row['error']}", file=sys.stderr, flush=True)
            save_record(db, row)
    finally:
        try:
            write_manifest(db, out)
        finally:
            db.close()
    print(f"\nDone. New: {ok}; reused: {skipped}; errors: {errors}; manifest: {out / 'manifest.csv'}", flush=True)
    return 2 if errors else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted. Rerun the same command to resume.", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)

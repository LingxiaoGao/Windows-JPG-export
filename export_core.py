"""v3 batch engine, extending the audited safari_arw_to_jpg v1/v2 workflow.

No RAW decoding. Extraction, orientation, atomic writes and SQLite resume derive
from embedded_legacy.py. Metadata is rebuilt from an explicit safe EXIF list.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from pathlib import Path
from collections import Counter, defaultdict
import csv
import hashlib
import io
import json
import os
import shutil
import sqlite3
import struct
import sys
import threading
import time
import uuid
from PIL import Image, ImageCms, ImageOps, ImageFilter
import embedded_legacy as legacy

VERSION = "3.0.0"
MODES = {"original": "JPEG_Original", "wechat": "JPEG_WeChat_2560", "share": "JPEG_Share_4096"}
EXIF_TAGS = (
    "Make", "Model", "DateTimeOriginal", "CreateDate", "OffsetTimeOriginal",
    "OffsetTimeDigitized", "SubSecTimeOriginal", "SubSecTimeDigitized", "LensMake",
    "LensModel", "LensInfo", "FocalLength", "FocalLengthIn35mmFormat", "FNumber",
    "ExposureTime", "ISO", "ExposureCompensation", "ExposureProgram", "MeteringMode",
    "Flash", "WhiteBalance", "ExposureMode", "SceneCaptureType",
)
FIELDS = (
    "source_path", "relative_path", "source_type", "source_size", "source_mtime_ns",
    "embedded_tag", "input_width", "input_height", "input_jpeg_bytes", "output_path",
    "output_width", "output_height", "output_bytes", "mode", "resized", "rotated",
    "reencoded", "icc_converted", "jpeg_quality", "quality_note", "gps_removed",
    "resolution_below_target", "size_over_limit", "status", "error", "processed_at",
    "elapsed_seconds", "byte_identical", "metadata_only", "orientation", "orientation_note",
    "color_note", "input_jpeg_sha256", "output_sha256", "warnings", "config_hash",
)


@dataclass
class Settings:
    mode: str = "wechat"
    long_edge: int = 2560
    quality: int = 95
    min_quality: int = 88
    max_mb: float = 4.0  # Decimal MB: 4,000,000 bytes, explicitly shown in UI.
    sharpen: bool = False
    keep_exif: bool = True
    keep_structure: bool = True
    existing: str = "resume"
    below_policy: str = "keep"
    orientation_source: str = "auto"

    @classmethod
    def preset(cls, mode):
        return cls(mode=mode, long_edge=4096 if mode == "share" else 2560,
                   max_mb=0 if mode in ("share", "original") else 4)

    def validate(self):
        if self.mode not in MODES or not 1 <= self.long_edge <= 30000:
            raise ValueError("导出模式或长边无效")
        if not 88 <= self.min_quality <= self.quality <= 95:
            raise ValueError("JPEG 质量必须在 88–95，最低质量不能高于起始质量")
        if not 0 <= self.max_mb <= 1000:
            raise ValueError("文件大小上限无效")
        if self.existing not in ("resume", "replace", "skip") or self.below_policy not in ("keep", "skip"):
            raise ValueError("文件处理策略无效")


@dataclass
class Photo:
    path: Path
    relative: Path
    output_relative: Path


class Cancelled(Exception):
    pass


class Control:
    def __init__(self):
        self.cancelled = threading.Event()
        self.paused = threading.Event()
        self.pause_time = 0.0

    def checkpoint(self):
        started = time.monotonic()
        while self.paused.is_set() and not self.cancelled.is_set():
            self.cancelled.wait(.15)
        self.pause_time += time.monotonic() - started
        if self.cancelled.is_set():
            raise Cancelled()


def resource_root():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def exiftool_path():
    bundled = resource_root() / "resources" / "exiftool" / "exiftool.exe"
    if bundled.is_file():
        return str(bundled)
    if getattr(sys, "frozen", False):
        raise RuntimeError("内置 ExifTool 缺失，请完整解压便携 ZIP")
    return legacy.get_exiftool(r"C:\Tools\ExifTool\exiftool.exe")[0]


def tool(exe, *args):
    return legacy.run_exiftool(exe, *args)


def metadata(exe, src):
    payload, warn = tool(exe, "-j", "-n", "-G1", "-IFD0:Orientation", "-IFD0:ImageWidth",
                         "-IFD0:ImageHeight", "-ExifIFD:ExifImageWidth", "-ExifIFD:ExifImageHeight",
                         "-ExifIFD:ColorSpace", "-InteropIFD:InteropIndex", src)
    records = json.loads(payload.decode("utf-8"))
    if len(records) != 1 or any(k.endswith(":Error") for k in records[0]):
        raise RuntimeError(f"无法读取元数据：{records}")
    return records[0], warn


def scan(inp: Path, out: Path, selection="all", structure=True):
    inp, out = inp.resolve(), out.resolve()
    legacy.verify_paths(inp, out)
    sources = sorted((p for p in inp.rglob("*") if p.is_file() and p.suffix.lower() in (".arw", ".jpg", ".jpeg")),
                     key=lambda p: (str(p).casefold(), str(p)))
    group = defaultdict(list)
    pairs = defaultdict(set)
    for path in sources:
        rel = path.relative_to(inp)
        group[(rel.parent.as_posix().casefold() if structure else "", rel.stem.casefold())].append(path)
        pairs[(rel.parent.as_posix().casefold(), rel.stem.casefold())].add("RAW" if path.suffix.lower() == ".arw" else "JPG")
    mapped = []
    for path in sources:
        rel = path.relative_to(inp)
        key = (rel.parent.as_posix().casefold() if structure else "", rel.stem.casefold())
        name = rel.stem + ("_" + path.suffix[1:].upper() if len(group[key]) > 1 else "") + ".jpg"
        mapped.append(Photo(path, rel, (rel.parent if structure else Path()) / name))
    collisions = Counter(p.output_relative.as_posix().casefold() for p in mapped)
    # Always include relative-path hash for secondary collisions; no numbered suffixes.
    used = set()
    for photo in mapped:
        name = photo.output_relative
        if collisions[name.as_posix().casefold()] > 1:
            name = name.with_name(name.stem + "_" + hashlib.sha256(photo.relative.as_posix().encode()).hexdigest()[:12] + ".jpg")
        while name.as_posix().casefold() in used:
            name = name.with_name(name.stem + "_" + hashlib.sha256(photo.relative.as_posix().encode()).hexdigest()[:16] + ".jpg")
        photo.output_relative = name
        used.add(name.as_posix().casefold())
    stats = dict(arw=sum(p.suffix.lower() == ".arw" for p in sources),
                 jpg=sum(p.suffix.lower() != ".arw" for p in sources), total=len(sources),
                 pairs=sum(v == {"RAW", "JPG"} for v in pairs.values()))
    filtered = [p for p in mapped if selection == "all" or
                (p.path.suffix.lower() == ".arw") == (selection == "raw")]
    return filtered, stats


def input_jpeg(exe, src: Path, full=True):
    from PIL import UnidentifiedImageError
    meta, warning = metadata(exe, src)
    warnings = [warning] if warning else []
    if src.suffix.lower() == ".arw":
        if full:
            c, notes = legacy.choose_embedded(exe, src, 0)
            warnings.extend(notes)
        else:
            candidates = []
            for tag in legacy.TAGS:
                try:
                    payload, note = tool(exe, "-b", "-" + tag, src)
                    if not payload:
                        continue
                    c = inspect_jpeg(payload, tag, False)
                    candidates.append(c)
                    if note:
                        warnings.append(note)
                except (OSError, ValueError, RuntimeError, UnidentifiedImageError) as exc:
                    warnings.append(f"{tag}: {exc}")
            if not candidates:
                raise RuntimeError("ARW 中没有可读取的有效 JPEG 头；禁止 RAW 解码回退")
            c = max(candidates, key=lambda c: (c.width * c.height, max(c.width, c.height)))
    else:
        # Ordinary JPEG does not pass through the RAW extraction path.
        c = inspect_jpeg(src.read_bytes(), "source_jpeg", full)
    return c, meta, warnings


def inspect_jpeg(payload, tag, full):
    with Image.open(io.BytesIO(payload)) as im:
        if im.format != "JPEG":
            raise ValueError("输入不是 JPEG")
        if full:
            im.load()
        exif = im.getexif()
        color = exif.get_ifd(34665).get(40961)
        return legacy.Embedded(payload, im.width, im.height, tag,
                               legacy.orientation_value(exif.get(274)), color)


def resolve(c, meta, mode, source="auto"):
    try:
        return legacy.resolve_orientation(c, meta, source)
    except RuntimeError as exc:
        if "Neither JPEG nor ARW" in str(exc):
            return 1, "方向缺失，假定像素已正向"
        if mode == "original":
            return None, "未确定方向：" + str(exc)
        raise


def a98_profile():
    """Own ICC v2 matrix profile for Adobe RGB (1998), D50 adapted colorants.

    Encoding parameters from AdobeRGB1998.pdf / CSS Color 4; not an Adobe binary.
    """
    def xyz(values):
        return b"XYZ \0\0\0\0" + b"".join(struct.pack(">i", round(v * 65536)) for v in values)
    tags = {
        b"rXYZ": xyz((.6097559, .3111242, .0194811)),
        b"gXYZ": xyz((.2052401, .6256560, .0608902)),
        b"bXYZ": xyz((.1492240, .0632197, .7448387)),
        b"wtpt": xyz((.9642, 1, .8249)),
        b"rTRC": b"curv\0\0\0\0" + struct.pack(">IH", 1, 563),
        b"gTRC": b"curv\0\0\0\0" + struct.pack(">IH", 1, 563),
        b"bTRC": b"curv\0\0\0\0" + struct.pack(">IH", 1, 563),
    }
    name = b"A98 RGB compatible matrix profile\0"
    tags[b"desc"] = b"desc\0\0\0\0" + struct.pack(">I", len(name)) + name + b"\0" * 78
    tags[b"cprt"] = b"text\0\0\0\0Generated by Windows JPG Exporter; MIT\0"
    header = bytearray(128)
    header[8:12] = struct.pack(">I", 0x02100000)
    header[12:24] = b"mntrRGB XYZ "
    header[24:36] = struct.pack(">6H", 2026, 10, 12, 0, 0, 0)
    header[36:40] = b"acsp"
    header[40:44] = b"MSFT"
    header[68:80] = b"".join(struct.pack(">i", round(v * 65536)) for v in (.9642, 1, .8249))
    table, payload = bytearray(), bytearray()
    offset = 128 + 4 + len(tags) * 12
    for sig, data in tags.items():
        table += sig + struct.pack(">II", offset + len(payload), len(data))
        payload += data + b"\0" * ((-len(data)) % 4)
    profile = header + struct.pack(">I", len(tags)) + table + payload
    profile[:4] = struct.pack(">I", len(profile))
    return bytes(profile)


def rgb_image(c, orientation, meta):
    target = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
    with Image.open(io.BytesIO(c.payload)) as source:
        embedded_icc = source.info.get("icc_profile")
        source.getexif()[274] = orientation
        with ImageOps.exif_transpose(source) as oriented:
            im = oriented.convert("RGB")
        rgb_original = source.mode == "RGB"
    try:
        converted = False
        if embedded_icc:
            profile = ImageCms.ImageCmsProfile(io.BytesIO(embedded_icc))
            # Recognized standard sRGB profiles permit preserving compressed pixels.
            description = ImageCms.getProfileDescription(profile).strip().lower()
            if "srgb" in description and rgb_original:
                note = "有效 sRGB ICC；保留色彩像素"
            else:
                changed = ImageCms.profileToProfile(im, profile, target, outputMode="RGB")
                im.close()
                im, converted, note = changed, True, "内嵌 ICC 转换至 sRGB"
        else:
            color = c.color_space if c.color_space is not None else meta.get("ExifIFD:ColorSpace")
            interop = meta.get("InteropIFD:InteropIndex")
            if color == 2 or interop == "R03":
                profile = ImageCms.ImageCmsProfile(io.BytesIO(a98_profile()))
                changed = ImageCms.profileToProfile(im, profile, target, outputMode="RGB")
                im.close()
                im, converted, note = changed, True, "无 ICC，依据 Adobe RGB 标记按 A98 转换至 sRGB"
            elif color == 1:
                note = "EXIF 指明 sRGB，无 ICC"
            else:
                note = "无 ICC / 未知色彩空间：假定 sRGB"
        if not rgb_original:
            converted = True
            note += "；转换图像模式至 RGB"
        return im, converted, note, target.tobytes()
    except Exception:
        im.close()
        raise


def safe_metadata(exe, src, destination, width, height, icc, keep):
    profile = destination.with_name(destination.name + ".icc")
    try:
        profile.write_bytes(icc)
        args = ["-overwrite_original", "-all="]
        if keep:
            args += ["-tagsfromfile", str(src)] + ["-EXIF:" + tag for tag in EXIF_TAGS]
        args += ["-Orientation#=1", "-ColorSpace#=1", f"-ExifImageWidth={width}",
                 f"-ExifImageHeight={height}", "-ICC_Profile<=" + str(profile), str(destination)]
        _, warning = tool(exe, *args)
        # No original blocks are copied: GPS, XMP, IPTC, comments and maker notes are gone.
        audit, _ = tool(exe, "-j", "-G1", "-GPS:all", "-XMP:all", "-IPTC:all", destination)
        record = json.loads(audit.decode("utf-8"))[0]
        if any(k.startswith(("GPS:", "XMP", "IPTC:")) for k in record):
            raise RuntimeError("输出隐私审计失败，拒绝发布该文件")
        return warning
    finally:
        profile.unlink(missing_ok=True)


def encode(im, quality, icc):
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality, subsampling=0, icc_profile=icc)
    return buf.getvalue()


def ensure_space(parent, needed):
    if shutil.disk_usage(parent).free < needed + 1024 * 1024:
        raise OSError(28, "输出磁盘可用空间不足")


def export_photo(photo, out, settings, exe, control):
    start = time.monotonic()
    control.checkpoint()
    stat = photo.path.stat()
    c, meta, warnings = input_jpeg(exe, photo.path, True)
    ori, ori_note = resolve(c, meta, settings.mode, settings.orientation_source)
    dest = out / photo.output_relative
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.resolve().is_relative_to(out.resolve()):
        raise ValueError("输出路径越界或指向目录外的链接")
    temp = dest.with_name(dest.name + "." + uuid.uuid4().hex + ".tmp.jpg")
    row = dict(source_path=str(photo.path), relative_path=photo.relative.as_posix(),
               source_type=photo.path.suffix[1:].upper(), source_size=stat.st_size,
               source_mtime_ns=stat.st_mtime_ns, embedded_tag=c.tag,
               input_width=c.width, input_height=c.height, input_jpeg_bytes=len(c.payload),
               output_path=str(dest), mode=settings.mode, orientation=ori or "",
               orientation_note=ori_note, input_jpeg_sha256=hashlib.sha256(c.payload).hexdigest(),
               status="ok", error="", processed_at=legacy.now(), gps_removed=False,
               resized=False, rotated=False, reencoded=False, icc_converted=False,
               jpeg_quality="", quality_note="保留输入编码；原始质量不可可靠推断",
               byte_identical=False, metadata_only=False, resolution_below_target=False,
               size_over_limit=False, warnings="")
    try:
        if settings.mode == "original":
            ensure_space(dest.parent, len(c.payload) * 2)
            temp.write_bytes(c.payload)
            row.update(output_width=c.width, output_height=c.height, byte_identical=True,
                       color_note="原样字节；可能含 GPS；方向不改写")
        else:
            below = max(c.width, c.height) < settings.long_edge
            row["resolution_below_target"] = below
            if below and settings.below_policy == "skip":
                row.update(status="skipped", error="按用户设置跳过低于目标分辨率的照片")
                return row
            im, color_changed, note, icc = rgb_image(c, ori, meta)
            try:
                resize = max(im.size) > settings.long_edge
                if resize:
                    im.thumbnail((settings.long_edge, settings.long_edge), Image.Resampling.LANCZOS)
                if settings.sharpen:
                    sharpened = im.filter(ImageFilter.UnsharpMask(radius=.6, percent=50, threshold=3))
                    im.close()
                    im = sharpened
                row.update(resized=resize, rotated=ori != 1, icc_converted=color_changed,
                           color_note=note, output_width=im.width, output_height=im.height)
                need_encode = resize or ori != 1 or color_changed or settings.sharpen
                cap = round(settings.max_mb * 1_000_000) if settings.max_mb else 0
                quality = settings.quality
                while True:
                    control.checkpoint()
                    payload = encode(im, quality, icc) if need_encode else c.payload
                    ensure_space(dest.parent, len(payload) * 2)
                    temp.write_bytes(payload)
                    metadata_warning = safe_metadata(exe, photo.path, temp, im.width, im.height, icc, settings.keep_exif)
                    if metadata_warning:
                        warnings.append(metadata_warning)
                    actual = temp.stat().st_size
                    if not cap or actual <= cap or (need_encode and quality <= settings.min_quality):
                        break
                    if not need_encode:
                        need_encode = True
                    else:
                        quality -= 1
                row.update(reencoded=need_encode, metadata_only=not need_encode,
                           jpeg_quality=quality if need_encode else "",
                           quality_note="本次实际编码质量" if need_encode else row["quality_note"],
                           gps_removed=True, size_over_limit=bool(cap and actual > cap))
            finally:
                im.close()
        control.checkpoint()
        # Check complete JPEG and dimensions before atomic publication.
        with Image.open(temp) as check:
            check.load()
            if check.format != "JPEG" or check.size != (row["output_width"], row["output_height"]):
                raise RuntimeError("输出完整性检查失败")
            if settings.mode != "original" and check.getexif().get(274) != 1:
                raise RuntimeError("输出方向检查失败")
        final_stat = photo.path.stat()
        if (final_stat.st_size, final_stat.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
            raise RuntimeError("源文件在处理期间发生变化；请停止其他修改照片的程序后重试")
        with temp.open("r+b") as f:
            os.fsync(f.fileno())
        row["output_bytes"] = temp.stat().st_size
        row["output_sha256"] = hashlib.sha256(temp.read_bytes()).hexdigest()
        os.replace(temp, dest)
        row.update(elapsed_seconds=round(time.monotonic() - start, 3), warnings="; ".join(warnings))
        return row
    finally:
        temp.unlink(missing_ok=True)


def config_hash(settings, inp, exe, photo):
    data = dict(settings=asdict(settings), input=str(inp.resolve()), version=VERSION,
                executable=str(exe), output=photo.output_relative.as_posix())
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def write_reports(db, out, summary):
    rows = [json.loads(r[0]) for r in db.execute("SELECT data FROM exports ORDER BY relative_path")]
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    legacy.atomic_bytes(out / "manifest.csv", buf.getvalue().encode("utf-8-sig"))
    errors = "\n".join(f"{r.get('processed_at', '')} {r['relative_path']}: {r.get('error', '')}" for r in rows if r.get("status") == "error")
    legacy.atomic_bytes(out / "errors.log", errors.encode("utf-8"))
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(("metric", "value"))
    writer.writerows(summary.items())
    legacy.atomic_bytes(out / "summary.csv", buf.getvalue().encode("utf-8-sig"))


def retryable(exc):
    return (isinstance(exc, (OSError, PermissionError)) and
            (getattr(exc, "winerror", None) in (5, 21, 32, 33, 1167) or getattr(exc, "errno", None) in (13, 16)))


def batch(photos, inp, out_root, settings, control, callback=lambda event, data: None, exe=None):
    settings.validate()
    legacy.verify_paths(inp.resolve(), out_root.resolve())
    exe = exe or exiftool_path()
    out = (out_root / MODES[settings.mode]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(out / "progress.sqlite3")
    db.execute("CREATE TABLE IF NOT EXISTS exports (relative_path TEXT PRIMARY KEY, data TEXT NOT NULL)")
    db.commit()
    summary = dict(total=len(photos), completed=0, success=0, skipped=0, failed=0,
                   resolution_below_target=0, size_over_limit=0, cancelled=False,
                   output=str(out), mode=settings.mode, version=VERSION)
    started = time.monotonic()
    try:
        for photo in photos:
            control.checkpoint()
            callback("current", dict(file=photo.relative.as_posix(), **summary))
            signature = config_hash(settings, inp, exe, photo)
            previous = db.execute("SELECT data FROM exports WHERE relative_path=?", (photo.relative.as_posix(),)).fetchone()
            previous = json.loads(previous[0]) if previous else None
            stat = None
            try:
                stat = photo.path.stat()
                dest = out / photo.output_relative
                valid_cache = bool(previous and previous.get("status") == "ok"
                    and previous.get("config_hash") == signature
                    and previous.get("source_path") == str(photo.path)
                    and previous.get("source_size") == stat.st_size
                    and previous.get("source_mtime_ns") == stat.st_mtime_ns
                    and dest.is_file() and dest.stat().st_size == previous.get("output_bytes")
                    and hashlib.sha256(dest.read_bytes()).hexdigest() == previous.get("output_sha256"))
                if (settings.existing == "resume" and valid_cache):
                    row = previous.copy()
                    outcome = "skipped"
                elif settings.existing == "skip" and dest.exists():
                    row = dict(relative_path=photo.relative.as_posix(), source_path=str(photo.path),
                               status="skipped", error="用户选择跳过已有输出；未验证其正确性",
                               output_path=str(dest), mode=settings.mode, processed_at=legacy.now())
                    outcome = "skipped"
                else:
                    for attempt in range(3):
                        try:
                            row = export_photo(photo, out, settings, exe, control)
                            break
                        except Cancelled:
                            raise
                        except Exception as exc:
                            if not retryable(exc) or attempt == 2:
                                raise
                            callback("retry", dict(file=photo.relative.as_posix(), attempt=attempt + 1, error=str(exc)))
                            if control.cancelled.wait(.4 * (attempt + 1)):
                                raise Cancelled()
                    outcome = "skipped" if row["status"] == "skipped" else "success"
            except Cancelled:
                raise
            except Exception as exc:
                row = dict(relative_path=photo.relative.as_posix(), source_path=str(photo.path),
                           source_type=photo.path.suffix[1:].upper(), mode=settings.mode,
                           status="error", error=f"{type(exc).__name__}: {exc}", processed_at=legacy.now())
                if stat:
                    row.update(source_size=stat.st_size, source_mtime_ns=stat.st_mtime_ns)
                outcome = "failed"
            row["config_hash"] = signature
            db.execute("INSERT OR REPLACE INTO exports VALUES (?,?)", (photo.relative.as_posix(), json.dumps(row, ensure_ascii=False)))
            db.commit()
            summary["completed"] += 1
            summary[outcome] += 1
            summary["resolution_below_target"] += int(bool(row.get("resolution_below_target")))
            summary["size_over_limit"] += int(bool(row.get("size_over_limit")))
            elapsed = max(.01, time.monotonic() - started - control.pause_time)
            callback("progress", dict(row=row, speed=summary["completed"] * 60 / elapsed,
                remaining_seconds=(summary["total"] - summary["completed"]) * elapsed / summary["completed"], **summary))
            if summary["completed"] % 25 == 0:
                write_reports(db, out, summary)
    except Cancelled:
        summary["cancelled"] = True
    finally:
        try:
            write_reports(db, out, summary)
        finally:
            db.close()
    callback("done", summary)
    return summary


def preflight(photos, settings, control, callback=lambda event, data: None, exe=None):
    exe = exe or exiftool_path()
    result = dict(total=len(photos), provisional_ok=0, missing_jpeg=0, corrupt=0,
                  resolution_below_target=0, possible_over_limit_estimate=0, problems=[], details=[], cancelled=False)
    for index, photo in enumerate(photos, 1):
        try:
            control.checkpoint()
            c, meta, warnings = input_jpeg(exe, photo.path, False)
            ori, note = resolve(c, meta, settings.mode, settings.orientation_source)
            below = settings.mode != "original" and max(c.width, c.height) < settings.long_edge
            result["resolution_below_target"] += int(below)
            result["possible_over_limit_estimate"] += int(bool(settings.max_mb and len(c.payload) > settings.max_mb * 1_000_000))
            result["provisional_ok"] += 1
            result["details"].append(dict(file=photo.relative.as_posix(), tag=c.tag, width=c.width, height=c.height,
                                          jpeg_orientation=c.orientation, raw_orientation=meta.get("IFD0:Orientation"),
                                          orientation=ori, orientation_note=note, bytes=len(c.payload), warnings=warnings))
        except Cancelled:
            result["cancelled"] = True
            break
        except Exception as exc:
            key = "missing_jpeg" if photo.path.suffix.lower() == ".arw" and "JPEG" in str(exc) else "corrupt"
            result[key] += 1
            result["problems"].append(dict(file=photo.relative.as_posix(), error=str(exc)))
        callback("preflight_progress", dict(index=index, total=len(photos), file=photo.relative.as_posix()))
    callback("preflight_done", result)
    return result

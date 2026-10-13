"""Offline acceptance tests, also callable from the frozen EXE.

ARW fixtures are synthetic TIFF containers, not real Sony camera files.
"""
from __future__ import annotations
from pathlib import Path
from unittest.mock import patch
import io
import json
import hashlib
import random
import struct
import sys
import tempfile
import threading
import time
import unittest
from PIL import Image, ImageDraw, ImageCms
import export_core as core
DIAGNOSTICS = []


def jpeg(size=(1200, 800), orientation=1, icc=None, noise=False, quality=95):
    if noise:
        im = Image.frombytes("RGB", size, random.Random(77).randbytes(size[0] * size[1] * 3))
    else:
        im = Image.new("RGB", size)
        w, h = size
        draw = ImageDraw.Draw(im)
        for box, color in (((0, 0, w//2-1, h//2-1), (240, 10, 10)),
                           ((w//2, 0, w-1, h//2-1), (10, 240, 10)),
                           ((0, h//2, w//2-1, h-1), (10, 10, 240)),
                           ((w//2, h//2, w-1, h-1), (240, 240, 10))):
            draw.rectangle(box, fill=color)
    exif = Image.Exif()
    if orientation is not None:
        exif[274] = orientation
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality, subsampling=0, exif=exif.tobytes(), icc_profile=icc)
    im.close()
    return buf.getvalue()


def fixture(path, payload=None, orientation=6):
    payload = jpeg((3000, 2000), None) if payload is None else payload
    entries = [(0x100, 4, 1, 6000), (0x101, 4, 1, 4000), (0x10f, 2, 5, 0)]
    if orientation is not None:
        entries.append((0x112, 3, 1, orientation))
    entries += [(0x201, 4, 1, 0), (0x202, 4, 1, len(payload)), (0x8769, 4, 1, 0)]
    extra = 8 + 2 + len(entries) * 12 + 4
    exif_offset = extra + 6
    jpeg_offset = exif_offset + 18
    offsets = {0x10f: extra, 0x201: jpeg_offset, 0x8769: exif_offset}
    data = b"II" + struct.pack("<HIH", 42, 8, len(entries))
    for tag, kind, count, value in entries:
        data += struct.pack("<HHII", tag, kind, count, offsets.get(tag, value))
    data += struct.pack("<I", 0) + b"SONY\0\0" + struct.pack("<HHHIII", 1, 0xa001, 3, 1, 1, 0)
    assert len(data) == jpeg_offset
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data + payload)
    return payload


def image_scan(payload):
    """Return SOS+entropy+EOI for baseline JPEG, independent of metadata blocks."""
    i = 2
    while i < len(payload):
        if payload[i] != 255:
            raise ValueError("Bad marker")
        marker = payload[i+1]
        if marker == 0xda:
            return payload[i:]
        if marker in (0xd8, 0xd9):
            i += 2
        else:
            i += 2 + struct.unpack(">H", payload[i+2:i+4])[0]
    raise ValueError("No SOS")


def corners(im):
    w,h = im.size
    colors = ((240,10,10),(10,240,10),(10,10,240),(240,240,10))
    return [min(range(4), key=lambda n: sum((im.getpixel(p)[k]-colors[n][k])**2 for k in range(3)))
            for p in ((w//4,h//4),(3*w//4,h//4),(w//4,3*h//4),(3*w//4,3*h//4))]


class Acceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.exe = core.exiftool_path()
        cls.srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="jpg_export_acceptance_")
        self.root = Path(self.temp.name)
        self.inp = self.root / "照片 中文 空格"
        self.out = self.root / "导出"
        self.inp.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def photo(self, name, payload=None, raw=False, orientation=6):
        path = self.inp / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if raw:
            fixture(path, payload, orientation)
        else:
            path.write_bytes(payload if payload is not None else jpeg())
        return path

    def run_batch(self, settings=None, callback=lambda *args: None, control=None):
        settings = settings or core.Settings()
        photos, stats = core.scan(self.inp, self.out, structure=settings.keep_structure)
        rows = []
        def capture(event,data):
            if event == "progress":
                rows.append(data["row"])
            callback(event,data)
        summary = core.batch(photos, self.inp, self.out, settings, control or core.Control(), capture, self.exe)
        DIAGNOSTICS.extend(r for r in rows if r.get("status") == "error")
        return summary, rows

    def test_01_original_arw_byte_identical_and_source_unchanged(self):
        payload = jpeg((1600, 1000), None)
        path = self.photo("a.ARW", payload, raw=True)
        snap = hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns
        summary, rows = self.run_batch(core.Settings.preset("original"))
        self.assertEqual(summary["success"], 1)
        self.assertEqual(Path(rows[0]["output_path"]).read_bytes(), payload)
        self.assertTrue(rows[0]["byte_identical"])
        self.assertEqual(snap, (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns))
        self.assertFalse(any(n in sys.modules for n in ("rawpy", "libraw")))

    def test_02_all_eight_directions(self):
        payload = jpeg((120,80), None)
        c = core.inspect_jpeg(payload, "test", True)
        expected={1:[0,1,2,3],2:[1,0,3,2],3:[3,2,1,0],4:[2,3,0,1],
                  5:[0,2,1,3],6:[2,0,3,1],7:[3,1,2,0],8:[1,3,0,2]}
        for ori in range(1,9):
            im,changed,note,icc=core.rgb_image(c,ori,{})
            try:
                self.assertEqual(corners(im),expected[ori])
                self.assertEqual(im.size,(80,120) if ori>=5 else (120,80))
            finally:
                im.close()

    def test_03_small_arw_is_exported_without_upscale(self):
        self.photo("small.ARW", jpeg((1600,1000),None),raw=True)
        summary,rows=self.run_batch()
        self.assertEqual(summary["success"],1)
        self.assertEqual(summary["resolution_below_target"],1)
        self.assertEqual((rows[0]["output_width"],rows[0]["output_height"]),(1000,1600))
        self.assertEqual(rows[0]["jpeg_quality"],95)

    def test_04_raw_only(self):
        self.photo("raw.ARW",raw=True)
        summary,rows=self.run_batch()
        self.assertEqual(summary["success"],1)
        self.assertEqual((rows[0]["output_width"],rows[0]["output_height"]),(1707,2560))

    def test_05_jpg_only_and_metadata_only(self):
        payload=jpeg(icc=self.srgb)
        self.photo("c1_export.JPEG",payload)
        summary,rows=self.run_batch()
        self.assertEqual(summary["success"],1)
        row=rows[0]
        self.assertFalse(row["reencoded"])
        self.assertTrue(row["metadata_only"])
        self.assertFalse(row["byte_identical"])
        self.assertEqual(image_scan(Path(row["output_path"]).read_bytes()),image_scan(payload))

    def test_06_mixed_same_name_not_overwritten(self):
        self.photo("DSC01234.ARW",raw=True)
        self.photo("DSC01234.JPG")
        self.photo("DSC01234_ARW.JPG")
        photos,stats=core.scan(self.inp,self.out)
        self.assertEqual(stats,dict(arw=1,jpg=2,total=3,pairs=1))
        self.assertEqual(len({p.output_relative.as_posix().casefold() for p in photos}),3)
        summary,rows=self.run_batch()
        self.assertEqual(summary["success"],3)
        self.assertEqual(len({r["output_path"] for r in rows}),3)

    def test_07_a98_icc_conversion_changes_pixels(self):
        profile=core.a98_profile()
        im=Image.new("RGB",(80,60),(80,170,100))
        buf=io.BytesIO()
        im.save(buf,format="JPEG",quality=95,icc_profile=profile)
        im.close()
        c=core.inspect_jpeg(buf.getvalue(),"test",True)
        result,changed,note,icc=core.rgb_image(c,1,{})
        try:
            self.assertTrue(changed)
            self.assertNotEqual(result.getpixel((20,20)),(80,170,100))
            self.assertTrue(icc)
            self.assertIn("ICC 转换",note)
        finally:
            result.close()

    def test_08_gps_xmp_removed_and_capture_fields_kept(self):
        path=self.photo("gps.JPG")
        core.tool(self.exe,"-overwrite_original","-GPSLatitude=31.2","-GPSLatitudeRef=N",
            "-GPSLongitude=121.5","-GPSLongitudeRef=E","-XMP:GPSLatitude=31.2",
            "-XMP:Location=PRIVATE PLACE","-Comment=private location","-Make=SONY","-Model=ILCE-1",
            "-LensModel=Test 400mm","-DateTimeOriginal=2026:10:01 12:34:56","-FNumber=4",
            "-ExposureTime=1/1000","-ISO=800",path)
        summary,rows=self.run_batch()
        self.assertEqual(summary["success"],1)
        data,_=core.tool(self.exe,"-j","-G1","-GPS:all","-XMP:all","-IPTC:all","-Comment",
            "-Make","-Model","-DateTimeOriginal","-LensModel","-ISO",rows[0]["output_path"])
        record=json.loads(data)[0]
        self.assertFalse(any(k.startswith(("GPS:","XMP","IPTC:")) or k.endswith(":Comment") for k in record))
        self.assertEqual(record["IFD0:Make"],"SONY")
        self.assertEqual(record["IFD0:Model"],"ILCE-1")
        self.assertEqual(record["ExifIFD:ISO"],800)
        self.assertTrue(rows[0]["gps_removed"])

    def test_09_quality_reduction_respects_four_mb(self):
        self.photo("noise.JPG",jpeg((1400,1400),noise=True,quality=100))
        summary,rows=self.run_batch()
        self.assertEqual(summary["success"],1)
        self.assertTrue(rows[0]["reencoded"])
        self.assertTrue(88<=rows[0]["jpeg_quality"]<=95)
        self.assertLessEqual(rows[0]["output_bytes"],4_000_000)

    def test_10_q88_over_four_mb_preserves_2560(self):
        self.photo("big_noise.JPG",jpeg((2560,2560),noise=True,quality=100))
        summary,rows=self.run_batch()
        self.assertEqual(summary["success"],1)
        self.assertEqual(rows[0]["jpeg_quality"],88)
        self.assertEqual((rows[0]["output_width"],rows[0]["output_height"]),(2560,2560))
        self.assertGreater(rows[0]["output_bytes"],4_000_000)
        self.assertEqual(summary["size_over_limit"],1)

    def test_11_cancel_and_resume(self):
        self.photo("a.JPG")
        self.photo("b.JPG")
        control=core.Control()
        def cb(event,data):
            if event=="progress":control.cancelled.set()
        summary,rows=self.run_batch(callback=cb,control=control)
        self.assertTrue(summary["cancelled"])
        self.assertEqual(summary["completed"],1)
        summary,rows=self.run_batch()
        self.assertEqual((summary["skipped"],summary["success"]),(1,1))

    def test_12_corruption_does_not_stop_batch(self):
        self.photo("bad.ARW",b"badjpeg",raw=True)
        self.photo("good.JPG")
        summary,rows=self.run_batch()
        self.assertEqual((summary["failed"],summary["success"]),(1,1))

    def test_13_limited_lock_retries(self):
        self.photo("good.JPG")
        original=core.export_photo
        count=[0]
        def locked(*args,**kwargs):
            count[0]+=1
            if count[0]<3:raise PermissionError(13,"simulated lock")
            return original(*args,**kwargs)
        with patch.object(core,"export_photo",side_effect=locked):
            summary,rows=self.run_batch()
        self.assertEqual(summary["success"],1)
        self.assertEqual(count[0],3)

    def test_14_disk_full_no_success_or_partial_jpeg(self):
        self.photo("good.JPG")
        with patch.object(core,"ensure_space",side_effect=OSError(28,"simulated disk full")):
            summary,rows=self.run_batch()
        self.assertEqual(summary["failed"],1)
        self.assertFalse(list(self.out.rglob("*.jpg")))
        self.assertFalse(list(self.out.rglob("*.icc")))

    def test_15_nested_directories_rejected(self):
        for path in (self.inp,self.inp/"output",self.inp.parent):
            with self.assertRaises(ValueError):core.scan(self.inp,path)

    def test_16_config_changes_and_tampered_output_reprocessed(self):
        self.photo("good.JPG")
        summary,rows=self.run_batch()
        Path(rows[0]["output_path"]).write_bytes(b"wrong")
        summary,rows=self.run_batch()
        self.assertEqual(summary["success"],1)
        s=core.Settings(quality=94)
        summary,rows=self.run_batch(s)
        self.assertEqual(summary["success"],1)

    def test_17_original_jpg_is_exact_with_gps_kept(self):
        path=self.photo("orig.JPG")
        core.tool(self.exe,"-overwrite_original","-GPSLatitude=31.2","-GPSLatitudeRef=N",path)
        before=path.read_bytes()
        summary,rows=self.run_batch(core.Settings.preset("original"))
        self.assertEqual(Path(rows[0]["output_path"]).read_bytes(),before)
        self.assertFalse(rows[0]["gps_removed"])

    def test_18_preflight_does_not_decode_all_pixels(self):
        self.photo("small.ARW",jpeg((1200,800),None),raw=True)
        self.photo("good.JPG")
        photos,stats=core.scan(self.inp,self.out)
        original=core.inspect_jpeg
        flags=[]
        def inspect(payload,tag,full):
            flags.append(full)
            return original(payload,tag,full)
        with patch.object(core,"inspect_jpeg",side_effect=inspect):
            result=core.preflight(photos,core.Settings(),core.Control(),exe=self.exe)
        self.assertEqual(result["provisional_ok"],2)
        self.assertEqual(result["resolution_below_target"],2)
        self.assertTrue(flags)
        self.assertFalse(any(flags))

    def test_19_pause_resume(self):
        control=core.Control()
        control.paused.set()
        passed=threading.Event()
        thread=threading.Thread(target=lambda:(control.checkpoint(),passed.set()))
        thread.start()
        self.assertFalse(passed.wait(.2))
        control.paused.clear()
        self.assertTrue(passed.wait(2))
        thread.join()

    def test_20_flat_names_stable_unicode_and_share_preset(self):
        self.photo("目录一/a.JPG")
        self.photo("目录二/a.JPG")
        first,_=core.scan(self.inp,self.out,structure=False)
        second,_=core.scan(self.inp,self.out,structure=False)
        self.assertEqual([p.output_relative for p in first],[p.output_relative for p in second])
        self.assertEqual(len({p.output_relative for p in first}),2)
        s=core.Settings.preset("share")
        self.assertEqual((s.long_edge,s.quality,s.max_mb),(4096,95,0))
        summary,rows=self.run_batch(s)
        self.assertEqual(summary["success"],2)
        self.assertTrue(all(r["resolution_below_target"] for r in rows))


def run_self_test(target):
    target.mkdir(parents=True,exist_ok=True)
    stream=io.StringIO()
    result=unittest.TextTestRunner(stream=stream,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Acceptance))
    report=dict(version=core.VERSION,tests=result.testsRun,passed=result.wasSuccessful(),
                failures=len(result.failures),errors=len(result.errors),frozen=bool(getattr(sys,"frozen",False)),
                executable=sys.executable,exiftool=core.exiftool_path(),
                fixtures="Synthetic TIFF/JPEG; no real Sony ARW provided", diagnostics=DIAGNOSTICS)
    (target/"test_results.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    (target/"test_results.txt").write_text(stream.getvalue(),encoding="utf-8")
    return 0 if result.wasSuccessful() else 1


if __name__=="__main__":
    sys.exit(run_self_test(Path(sys.argv[1]) if len(sys.argv)>1 else Path("test_results")))

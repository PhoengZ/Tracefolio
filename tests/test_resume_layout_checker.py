from __future__ import annotations

import importlib.util
import json
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "tools" / "check_resume_layout.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_resume_layout", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    import sys

    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def _write_synthetic_png(
    path: Path,
    *,
    width: int,
    height: int,
    ink_box: tuple[int, int, int, int] | None,
    color_type: int = 2,
    filter_type: int = 0,
) -> Path:
    """Create a valid 8-bit PNG (RGB or RGBA) with a dark rectangle at ink_box."""
    bpp = 3 if color_type == 2 else 4
    white_px = (255, 255, 255) if bpp == 3 else (255, 255, 255, 255)
    ink_px = (20, 20, 20) if bpp == 3 else (20, 20, 20, 255)

    scanlines: list[bytes] = []
    prev_raw = bytearray(width * bpp)

    for y in range(height):
        raw = bytearray(width * bpp)
        for x in range(width):
            is_ink = (
                ink_box is not None
                and ink_box[0] <= x < ink_box[2]
                and ink_box[1] <= y < ink_box[3]
            )
            px = ink_px if is_ink else white_px
            offset = x * bpp
            raw[offset : offset + bpp] = bytes(px)

        if filter_type == 0:
            filtered = bytes(raw)
        elif filter_type == 1:  # Sub
            out = bytearray(len(raw))
            for i in range(len(raw)):
                left = raw[i - bpp] if i >= bpp else 0
                out[i] = (raw[i] - left) & 0xFF
            filtered = bytes(out)
        elif filter_type == 2:  # Up
            filtered = bytes((raw[i] - prev_raw[i]) & 0xFF for i in range(len(raw)))
        elif filter_type == 3:  # Average
            out = bytearray(len(raw))
            for i in range(len(raw)):
                left = raw[i - bpp] if i >= bpp else 0
                out[i] = (raw[i] - ((left + prev_raw[i]) >> 1)) & 0xFF
            filtered = bytes(out)
        elif filter_type == 4:  # Paeth
            out = bytearray(len(raw))
            for i in range(len(raw)):
                a = raw[i - bpp] if i >= bpp else 0
                b = prev_raw[i]
                c = prev_raw[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                out[i] = (raw[i] - pred) & 0xFF
            filtered = bytes(out)
        else:
            raise ValueError(f"Unsupported test filter {filter_type}")

        scanlines.append(bytes([filter_type]) + filtered)
        prev_raw = raw

    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    idat = zlib.compress(b"".join(scanlines))
    payload = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )
    path.write_bytes(payload)
    return path


def test_balanced_one_page_resume_passes(tmp_path: Path) -> None:
    checker = _load_checker()
    # 100 px/in synthetic US-Letter canvas: 850 x 1100
    # Top = 45 px (0.45 in), Bottom = 50 px (0.50 in), ratio = 1.11x, delta = +0.05 in
    _write_synthetic_png(
        tmp_path / "Candidate_CV_1.png",
        width=850,
        height=1100,
        ink_box=(45, 45, 805, 1050),
    )

    metrics = checker.check_resume_layout(tmp_path, force_stdlib=True)
    assert metrics.status == "PASS"
    assert metrics.recommendation == "PASS"
    assert metrics.png_count == 1
    assert metrics.top_in == 0.45
    assert metrics.bottom_in == 0.5
    assert metrics.bottom_to_top_ratio == 1.11
    assert metrics.failures == []


def test_bottom_whitespace_asymmetry_fails_with_clear_recommendation(tmp_path: Path) -> None:
    checker = _load_checker()
    # Mirrors observed bug: top=0.36in, bottom=1.19in -> ratio=3.31x
    _write_synthetic_png(
        tmp_path / "Candidate_CV_1.png",
        width=850,
        height=1100,
        ink_box=(45, 36, 805, 981),
    )

    metrics = checker.check_resume_layout(tmp_path, force_stdlib=True)
    assert metrics.status == "FAIL"
    assert metrics.recommendation == "ADD_CONTENT_OR_INCREASE_VERTICAL_BALANCE"
    assert metrics.bottom_to_top_ratio is not None and metrics.bottom_to_top_ratio > 3.0
    assert any("bottom/top ratio" in f for f in metrics.failures)
    assert any("|bottom - top|" in f for f in metrics.failures)


def test_moderate_bottom_excess_recommends_calibration(tmp_path: Path) -> None:
    checker = _load_checker()
    # top=0.38in, bottom=0.75in -> delta=+0.37in (< 0.55in), ratio=1.97x
    _write_synthetic_png(
        tmp_path / "Candidate_CV_1.png",
        width=850,
        height=1100,
        ink_box=(45, 38, 805, 1025),
    )

    metrics = checker.check_resume_layout(tmp_path, force_stdlib=True)
    assert metrics.status == "FAIL"
    assert metrics.recommendation == "CALIBRATE_SYMMETRIC_MARGINS_OR_SPACING"


def test_two_page_output_fails_tighten_to_one_page(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_synthetic_png(tmp_path / "Candidate_CV_1.png", width=85, height=110, ink_box=(5, 5, 80, 105))
    _write_synthetic_png(tmp_path / "Candidate_CV_2.png", width=85, height=110, ink_box=(5, 5, 80, 30))

    metrics = checker.check_resume_layout(tmp_path)
    assert metrics.status == "FAIL"
    assert metrics.png_count == 2
    assert metrics.recommendation == "TIGHTEN_TO_1_PAGE"


def test_bottom_crowding_and_top_crowding_are_rejected(tmp_path: Path) -> None:
    checker = _load_checker()
    crowded_bottom = _write_synthetic_png(
        tmp_path / "bottom_tight.png",
        width=850,
        height=1100,
        ink_box=(45, 48, 805, 1080),  # bottom=0.20in
    )
    res_bottom = checker.check_resume_layout(crowded_bottom, force_stdlib=True)
    assert res_bottom.status == "FAIL"
    assert res_bottom.recommendation == "REDUCE_BOTTOM_CROWDING"

    crowded_top = _write_synthetic_png(
        tmp_path / "top_tight.png",
        width=850,
        height=1100,
        ink_box=(45, 20, 805, 1075),  # top=0.20in, bottom=0.25in -> both too tight
    )
    res_top = checker.check_resume_layout(crowded_top, force_stdlib=True)
    assert res_top.status == "FAIL"


def test_stdlib_png_decoder_handles_all_filters_and_rgba(tmp_path: Path) -> None:
    checker = _load_checker()
    expected_box = (12, 18, 70, 92)
    for color_type in (2, 6):
        for filter_type in (0, 1, 2, 3, 4):
            png = _write_synthetic_png(
                tmp_path / f"ct{color_type}_f{filter_type}.png",
                width=85,
                height=110,
                ink_box=expected_box,
                color_type=color_type,
                filter_type=filter_type,
            )
            w, h, bbox = checker.measure_png_ink_bounds(png, force_stdlib=True)
            assert (w, h, bbox) == (85, 110, expected_box)


def test_cli_json_output_and_exit_code(tmp_path: Path, capsys) -> None:
    checker = _load_checker()
    ok_dir = tmp_path / "ok"
    ok_dir.mkdir()
    _write_synthetic_png(ok_dir / "CV_1.png", width=850, height=1100, ink_box=(45, 45, 805, 1052))

    code = checker.main([str(ok_dir), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "PASS"
    assert payload["top_in"] == 0.45
    assert payload["bottom_in"] == 0.48

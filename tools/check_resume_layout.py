"""Check 1-page RenderCV resume PNGs for Top-vs-Bottom whitespace balance.

Measures actual rendered ink bounds directly from PNG pixels so layout checks
match what human eyes see. Uses Pillow when installed and falls back to a
pure-stdlib PNG decoder so it runs with zero extra dependencies in CI.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path

PAGE_SIZES_IN: dict[str, tuple[float, float]] = {
    "us-letter": (8.5, 11.0),
    "a4": (8.2677, 11.6929),
}

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@dataclass(frozen=True)
class LayoutMetrics:
    target: str
    png_file: str | None
    png_count: int
    page_size: str
    width_px: int | None
    height_px: int | None
    top_px: int | None
    bottom_px: int | None
    left_px: int | None
    right_px: int | None
    top_in: float | None
    bottom_in: float | None
    left_in: float | None
    right_in: float | None
    top_pct: float | None
    bottom_pct: float | None
    ink_span_pct: float | None
    ink_bottom_pct: float | None
    bottom_to_top_ratio: float | None
    vertical_delta_in: float | None
    abs_vertical_delta_in: float | None
    status: str
    recommendation: str
    failures: list[str]


def _paeth_predictor(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _decode_png_stdlib(path: Path) -> tuple[int, int, list[bytearray]]:
    """Decode non-interlaced 8-bit PNG into grayscale rows (0..255)."""
    data = path.read_bytes()
    if len(data) < 8 or data[:8] != PNG_SIGNATURE:
        raise ValueError(f"Not a valid PNG file: {path}")

    pos = 8
    width = height = bit_depth = color_type = interlace = 0
    palette: list[tuple[int, int, int]] = []
    idat_chunks: list[bytes] = []

    while pos + 8 <= len(data):
        length, chunk_type = struct.unpack(">I4s", data[pos : pos + 8])
        pos += 8
        chunk_data = data[pos : pos + length]
        pos += length + 4  # skip CRC
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, _comp, _filt, interlace = struct.unpack(
                ">IIBBBBB", chunk_data
            )
        elif chunk_type == b"PLTE":
            palette = [
                (chunk_data[i], chunk_data[i + 1], chunk_data[i + 2])
                for i in range(0, len(chunk_data), 3)
            ]
        elif chunk_type == b"IDAT":
            idat_chunks.append(chunk_data)
        elif chunk_type == b"IEND":
            break

    if bit_depth != 8 or interlace != 0:
        raise ValueError(
            f"Unsupported PNG format (bit_depth={bit_depth}, interlace={interlace}): {path}"
        )

    channels_by_type = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
    if color_type not in channels_by_type:
        raise ValueError(f"Unsupported PNG color_type={color_type}: {path}")
    bpp = channels_by_type[color_type]
    stride = width * bpp

    raw = zlib.decompress(b"".join(idat_chunks))
    expected_len = height * (stride + 1)
    if len(raw) < expected_len:
        raise ValueError(f"Truncated PNG image data in {path}")

    prev = bytearray(stride)
    gray_rows: list[bytearray] = []
    offset = 0

    for _y in range(height):
        filter_type = raw[offset]
        offset += 1
        row = bytearray(raw[offset : offset + stride])
        offset += stride

        if filter_type == 1:  # Sub
            for i in range(bpp, stride):
                row[i] = (row[i] + row[i - bpp]) & 0xFF
        elif filter_type == 2:  # Up
            for i in range(stride):
                row[i] = (row[i] + prev[i]) & 0xFF
        elif filter_type == 3:  # Average
            for i in range(stride):
                left = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif filter_type == 4:  # Paeth
            for i in range(stride):
                left = row[i - bpp] if i >= bpp else 0
                up = prev[i]
                up_left = prev[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + _paeth_predictor(left, up, up_left)) & 0xFF
        elif filter_type != 0:
            raise ValueError(f"Unsupported PNG filter type {filter_type}: {path}")

        prev = row

        if color_type == 0:  # Grayscale
            gray = bytearray(row)
        elif color_type == 2:  # RGB
            gray = bytearray(width)
            for x in range(width):
                idx = x * 3
                gray[x] = (77 * row[idx] + 150 * row[idx + 1] + 29 * row[idx + 2]) >> 8
        elif color_type == 3:  # Indexed palette
            gray = bytearray(width)
            for x in range(width):
                r, g, b = palette[row[x]]
                gray[x] = (77 * r + 150 * g + 29 * b) >> 8
        elif color_type == 4:  # Grayscale + Alpha (composite over white)
            gray = bytearray(width)
            for x in range(width):
                g_val = row[x * 2]
                alpha = row[x * 2 + 1]
                gray[x] = (g_val * alpha + 255 * (255 - alpha)) // 255
        else:  # RGBA (composite over white)
            gray = bytearray(width)
            for x in range(width):
                idx = x * 4
                lum = (77 * row[idx] + 150 * row[idx + 1] + 29 * row[idx + 2]) >> 8
                alpha = row[idx + 3]
                gray[x] = (lum * alpha + 255 * (255 - alpha)) // 255

        gray_rows.append(gray)

    return width, height, gray_rows


def _ink_bounds_pil(path: Path, ink_threshold: int) -> tuple[int, int, tuple[int, int, int, int] | None]:
    from PIL import Image

    with Image.open(path) as img:
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            gray = Image.alpha_composite(white, rgba).convert("L")
        else:
            gray = img.convert("L")
        width, height = gray.size
        mask = gray.point(lambda p: 255 if p < ink_threshold else 0)
        bbox = mask.getbbox()  # (left, top, right_exclusive, bottom_exclusive)
        return width, height, bbox


def _ink_bounds_stdlib(
    path: Path, ink_threshold: int
) -> tuple[int, int, tuple[int, int, int, int] | None]:
    width, height, gray_rows = _decode_png_stdlib(path)
    top: int | None = None
    bottom: int | None = None
    left = width
    right = -1

    for y, row in enumerate(gray_rows):
        row_min = min(row) if row else 255
        if row_min < ink_threshold:
            if top is None:
                top = y
            bottom = y
            for x, val in enumerate(row):
                if val < ink_threshold:
                    left = min(left, x)
                    break
            for x in range(width - 1, -1, -1):
                if row[x] < ink_threshold:
                    right = max(right, x)
                    break

    if top is None or bottom is None or right < left:
        return width, height, None
    return width, height, (left, top, right + 1, bottom + 1)


def measure_png_ink_bounds(
    path: Path, *, ink_threshold: int = 245, force_stdlib: bool = False
) -> tuple[int, int, tuple[int, int, int, int] | None]:
    if not force_stdlib:
        try:
            return _ink_bounds_pil(path, ink_threshold)
        except ImportError:
            pass
    return _ink_bounds_stdlib(path, ink_threshold)


def check_resume_layout(
    target: Path,
    *,
    page_size: str = "us-letter",
    ink_threshold: int = 245,
    min_margin_in: float = 0.32,
    max_bottom_to_top_ratio: float = 1.45,
    max_vertical_delta_in: float = 0.22,
    max_bottom_tighter_than_top_in: float = 0.12,
    force_stdlib: bool = False,
) -> LayoutMetrics:
    if page_size not in PAGE_SIZES_IN:
        raise ValueError(f"Unsupported page_size={page_size!r}; expected one of {list(PAGE_SIZES_IN)}")

    page_w_in, page_h_in = PAGE_SIZES_IN[page_size]

    if target.is_dir():
        pngs = sorted(target.glob("*.png"))
        png_count = len(pngs)
        if png_count == 0:
            return LayoutMetrics(
                target=str(target),
                png_file=None,
                png_count=0,
                page_size=page_size,
                width_px=None,
                height_px=None,
                top_px=None,
                bottom_px=None,
                left_px=None,
                right_px=None,
                top_in=None,
                bottom_in=None,
                left_in=None,
                right_in=None,
                top_pct=None,
                bottom_pct=None,
                ink_span_pct=None,
                ink_bottom_pct=None,
                bottom_to_top_ratio=None,
                vertical_delta_in=None,
                abs_vertical_delta_in=None,
                status="FAIL",
                recommendation="MISSING_PNG",
                failures=["No rendered PNG files found in target directory."],
            )
        if png_count != 1:
            return LayoutMetrics(
                target=str(target),
                png_file=pngs[0].name,
                png_count=png_count,
                page_size=page_size,
                width_px=None,
                height_px=None,
                top_px=None,
                bottom_px=None,
                left_px=None,
                right_px=None,
                top_in=None,
                bottom_in=None,
                left_in=None,
                right_in=None,
                top_pct=None,
                bottom_pct=None,
                ink_span_pct=None,
                ink_bottom_pct=None,
                bottom_to_top_ratio=None,
                vertical_delta_in=None,
                abs_vertical_delta_in=None,
                status="FAIL",
                recommendation="TIGHTEN_TO_1_PAGE",
                failures=[
                    f"Expected exactly 1 PNG page, found {png_count}: {[p.name for p in pngs]}."
                ],
            )
        png_path = pngs[0]
    else:
        if not target.is_file():
            raise FileNotFoundError(f"Target path does not exist: {target}")
        png_path = target
        png_count = 1

    width_px, height_px, bbox = measure_png_ink_bounds(
        png_path, ink_threshold=ink_threshold, force_stdlib=force_stdlib
    )
    if bbox is None:
        return LayoutMetrics(
            target=str(target),
            png_file=png_path.name,
            png_count=png_count,
            page_size=page_size,
            width_px=width_px,
            height_px=height_px,
            top_px=height_px,
            bottom_px=height_px,
            left_px=width_px,
            right_px=width_px,
            top_in=round(page_h_in, 3),
            bottom_in=round(page_h_in, 3),
            left_in=round(page_w_in, 3),
            right_in=round(page_w_in, 3),
            top_pct=100.0,
            bottom_pct=100.0,
            ink_span_pct=0.0,
            ink_bottom_pct=0.0,
            bottom_to_top_ratio=None,
            vertical_delta_in=0.0,
            abs_vertical_delta_in=0.0,
            status="FAIL",
            recommendation="BLANK_PAGE",
            failures=["Rendered PNG contains no visible ink pixels."],
        )

    left_px, top_px, right_excl, bottom_excl = bbox
    right_px = width_px - right_excl
    bottom_px = height_px - bottom_excl

    top_in = top_px / height_px * page_h_in
    bottom_in = bottom_px / height_px * page_h_in
    left_in = left_px / width_px * page_w_in
    right_in = right_px / width_px * page_w_in

    top_pct = top_px / height_px * 100.0
    bottom_pct = bottom_px / height_px * 100.0
    ink_span_pct = (bottom_excl - top_px) / height_px * 100.0
    ink_bottom_pct = bottom_excl / height_px * 100.0

    ratio = bottom_in / top_in if top_in > 0 else float("inf")
    delta_in = bottom_in - top_in
    abs_delta_in = abs(delta_in)

    failures: list[str] = []
    recommendation = "PASS"

    if top_in < min_margin_in:
        failures.append(f"top_in={top_in:.3f}in is below min_margin_in={min_margin_in:.2f}in.")
    if bottom_in < min_margin_in:
        failures.append(f"bottom_in={bottom_in:.3f}in is below min_margin_in={min_margin_in:.2f}in.")
    if delta_in < -max_bottom_tighter_than_top_in:
        failures.append(
            f"bottom_in={bottom_in:.3f}in is tighter than top_in={top_in:.3f}in by "
            f"{-delta_in:.3f}in (max allowed {max_bottom_tighter_than_top_in:.2f}in)."
        )
    if ratio > max_bottom_to_top_ratio:
        failures.append(
            f"bottom/top ratio={ratio:.2f}x exceeds max={max_bottom_to_top_ratio:.2f}x "
            f"(top={top_in:.3f}in vs bottom={bottom_in:.3f}in)."
        )
    if abs_delta_in > max_vertical_delta_in:
        failures.append(
            f"|bottom - top|={abs_delta_in:.3f}in exceeds max={max_vertical_delta_in:.2f}in "
            f"(top={top_in:.3f}in vs bottom={bottom_in:.3f}in)."
        )

    if failures:
        if bottom_in < min_margin_in or delta_in < -max_bottom_tighter_than_top_in:
            recommendation = "REDUCE_BOTTOM_CROWDING"
        elif top_in < min_margin_in:
            recommendation = "ADJUST_TOP_MARGIN"
        elif delta_in >= 0.55:
            recommendation = "ADD_CONTENT_OR_INCREASE_VERTICAL_BALANCE"
        else:
            recommendation = "CALIBRATE_SYMMETRIC_MARGINS_OR_SPACING"

    return LayoutMetrics(
        target=str(target),
        png_file=png_path.name,
        png_count=png_count,
        page_size=page_size,
        width_px=width_px,
        height_px=height_px,
        top_px=top_px,
        bottom_px=bottom_px,
        left_px=left_px,
        right_px=right_px,
        top_in=round(top_in, 3),
        bottom_in=round(bottom_in, 3),
        left_in=round(left_in, 3),
        right_in=round(right_in, 3),
        top_pct=round(top_pct, 2),
        bottom_pct=round(bottom_pct, 2),
        ink_span_pct=round(ink_span_pct, 2),
        ink_bottom_pct=round(ink_bottom_pct, 2),
        bottom_to_top_ratio=round(ratio, 2) if ratio != float("inf") else None,
        vertical_delta_in=round(delta_in, 3),
        abs_vertical_delta_in=round(abs_delta_in, 3),
        status="PASS" if not failures else "FAIL",
        recommendation=recommendation,
        failures=failures,
    )


def _format_human(metrics: LayoutMetrics) -> str:
    if metrics.top_in is None or metrics.bottom_in is None:
        lines = [
            f"[{metrics.status}] {metrics.target} (png_count={metrics.png_count}, rec={metrics.recommendation})"
        ]
    else:
        lines = [
            f"[{metrics.status}] {metrics.target} ({metrics.png_file}, {metrics.width_px}x{metrics.height_px})",
            (
                f"  top={metrics.top_in:.3f}in ({metrics.top_pct:.2f}%) | "
                f"bottom={metrics.bottom_in:.3f}in ({metrics.bottom_pct:.2f}%) | "
                f"bottom/top={metrics.bottom_to_top_ratio:.2f}x | "
                f"delta={metrics.vertical_delta_in:+.3f}in | "
                f"ink_span={metrics.ink_span_pct:.2f}% | "
                f"rec={metrics.recommendation}"
            ),
        ]
    for msg in metrics.failures:
        lines.append(f"  - {msg}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify 1-page RenderCV resume PNGs for Top-vs-Bottom whitespace symmetry."
    )
    parser.add_argument("targets", nargs="+", type=Path, help="Output directory or PNG file path(s).")
    parser.add_argument("--page-size", choices=sorted(PAGE_SIZES_IN), default="us-letter")
    parser.add_argument("--ink-threshold", type=int, default=245)
    parser.add_argument("--min-margin-in", type=float, default=0.32)
    parser.add_argument("--max-bottom-to-top-ratio", type=float, default=1.45)
    parser.add_argument("--max-vertical-delta-in", type=float, default=0.22)
    parser.add_argument("--max-bottom-tighter-than-top-in", type=float, default=0.12)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    results = [
        check_resume_layout(
            target,
            page_size=args.page_size,
            ink_threshold=args.ink_threshold,
            min_margin_in=args.min_margin_in,
            max_bottom_to_top_ratio=args.max_bottom_to_top_ratio,
            max_vertical_delta_in=args.max_vertical_delta_in,
            max_bottom_tighter_than_top_in=args.max_bottom_tighter_than_top_in,
        )
        for target in args.targets
    ]

    if args.as_json:
        payload = [asdict(item) for item in results]
        print(json.dumps(payload[0] if len(payload) == 1 else payload, indent=2))
    else:
        for item in results:
            print(_format_human(item))

    return 0 if all(item.status == "PASS" for item in results) else 1


if __name__ == "__main__":
    sys.exit(main())

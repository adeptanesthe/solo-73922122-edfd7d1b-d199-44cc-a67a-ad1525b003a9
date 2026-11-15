"""TIFF 解析与净化重建（纯标准库，明文只在内存中处理）。

输入：解密后的归档原件字节（TIFF/Classic TIFF，小端 II 或大端 MM）。
输出：仅含必要结构标签与标定标签的全新 8 位灰度 TIFF，逐页保留解码后的
灰度像素、页序以及完整有效的 XResolution/YResolution/ResolutionUnit；
不带出任何其他源标签或子目录（SubIFD）。

支持范围（题目约束）：
- 1～3 页（IFD），每页宽高 64～512 像素、8 位灰度（每像素 1 样本、每样本 8 位）；
- 仅支持未压缩（Compression=1）基线 TIFF，条带数据完整可解码；
- 方向必须为默认（Orientation 缺省或 1），非默认一律拒绝；
- 标定标签三枚必须同时存在且有效，缺一即“残缺”拒绝；三枚全缺可处理并注明；
- 任一页损坏/越界/类型不符时抛出 TiffError（含页号与原因），整件不出半成品。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

# TIFF 标签号
TAG_IMAGE_WIDTH = 256
TAG_IMAGE_LENGTH = 257
TAG_BITS_PER_SAMPLE = 258
TAG_COMPRESSION = 259
TAG_PHOTOMETRIC = 262
TAG_STRIP_OFFSETS = 273
TAG_ORIENTATION = 274
TAG_SAMPLES_PER_PIXEL = 277
TAG_ROWS_PER_STRIP = 278
TAG_STRIP_BYTE_COUNTS = 279
TAG_X_RESOLUTION = 282
TAG_Y_RESOLUTION = 283
TAG_PLANAR_CONFIG = 284
TAG_RESOLUTION_UNIT = 296

# TIFF 字段类型：(struct 单值格式, 字节数)；ASCII 单独处理
_TYPE_INFO: dict[int, tuple[str, int]] = {
    1: ("B", 1),   # BYTE
    3: ("H", 2),   # SHORT
    4: ("I", 4),   # LONG
    5: ("II", 8),  # RATIONAL（两个 LONG）
    6: ("b", 1),   # SBYTE
    8: ("h", 2),   # SSHORT
    9: ("i", 4),   # SLONG
    10: ("ii", 8),  # SRATIONAL
}

MIN_SIDE = 64
MAX_SIDE = 512
MAX_PAGES = 3


class TiffError(ValueError):
    """TIFF 不可净化：消息为可展示给用户的中文原因。"""


@dataclass
class Page:
    index: int                      # 从 1 开始的页号
    width: int
    length: int
    pixels: bytes                   # 解码后的灰度像素，width*length 字节
    x_resolution: tuple[int, int] | None = None
    y_resolution: tuple[int, int] | None = None
    resolution_unit: int | None = None
    has_calibration: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class SanitizedTiff:
    content: bytes
    page_count: int
    notes: list[str]                # 逐页注明（与页序对齐）


# ---------------------------------------------------------------------- #
# 解析
# ---------------------------------------------------------------------- #
def _read_value(entry_type: int, count: int, payload: bytes, endian: str):
    if entry_type == 2:  # ASCII
        if count == 0:
            return b""
        raw = payload[:count]
        if raw and raw[-1:] == b"\x00":
            raw = raw[:-1]
        return raw
    if entry_type not in _TYPE_INFO:
        raise TiffError(f"含不支持的字段类型 {entry_type}")
    fmt_char, unit = _TYPE_INFO[entry_type]
    if len(payload) < unit * count:
        raise TiffError("字段数据越界，文件已损坏")
    fmt = endian + fmt_char * count
    values = struct.unpack(fmt, payload[: unit * count])
    if entry_type == 5:  # RATIONAL -> (numerator, denominator) 元组
        return [(values[i], values[i + 1]) for i in range(0, len(values), 2)]
    return list(values)


def _parse_ifd(data: bytes, offset: int, endian: str, page_no: int) -> tuple[dict[int, tuple[int, int, object]], int]:
    """解析单个 IFD，返回 {tag: (type, count, values)} 与下一个 IFD 偏移。"""
    if offset <= 0 or offset + 2 > len(data):
        raise TiffError(f"第 {page_no} 页 IFD 偏移越界，文件已损坏")
    (entry_count,) = struct.unpack_from(endian + "H", data, offset)
    if entry_count == 0:
        raise TiffError(f"第 {page_no} 页 IFD 为空，文件已损坏")
    table_start = offset + 2
    table_end = table_start + entry_count * 12
    if table_end + 4 > len(data):
        raise TiffError(f"第 {page_no} 页 IFD 表越界，文件已损坏")

    tags: dict[int, tuple[int, int, object]] = {}
    for i in range(entry_count):
        entry = data[table_start + i * 12 : table_start + (i + 1) * 12]
        tag, entry_type, count = struct.unpack(endian + "HHI", entry[:8])
        value_blob = entry[8:12]
        if entry_type not in _TYPE_INFO and entry_type != 2:
            # 未知类型无法安全解释，按损坏处理
            raise TiffError(f"第 {page_no} 页标签 {tag} 使用不支持的字段类型 {entry_type}")
        unit = 1 if entry_type == 2 else _TYPE_INFO[entry_type][1]
        total = unit * count
        if total > 4:
            (external,) = struct.unpack(endian + "I", value_blob)
            if external + total > len(data):
                raise TiffError(f"第 {page_no} 页标签 {tag} 的值偏移越界，文件已损坏")
            payload = data[external : external + total]
        else:
            payload = value_blob[:total]
        if tag in tags:
            raise TiffError(f"第 {page_no} 页标签 {tag} 重复，文件已损坏")
        tags[tag] = (entry_type, count, _read_value(entry_type, count, payload, endian))

    (next_offset,) = struct.unpack_from(endian + "I", data, table_end)
    return tags, next_offset


def _single_short(tags: dict, tag: int, page_no: int, default=None):
    if tag not in tags:
        return default
    entry_type, count, values = tags[tag]
    if entry_type != 3 or count != 1:
        name = _tag_name(tag)
        raise TiffError(f"第 {page_no} 页 {name} 标签类型无效（要求单个 SHORT）")
    return values[0]


def _tag_name(tag: int) -> str:
    return {
        TAG_IMAGE_WIDTH: "ImageWidth",
        TAG_IMAGE_LENGTH: "ImageLength",
        TAG_BITS_PER_SAMPLE: "BitsPerSample",
        TAG_COMPRESSION: "Compression",
        TAG_PHOTOMETRIC: "PhotometricInterpretation",
        TAG_STRIP_OFFSETS: "StripOffsets",
        TAG_ORIENTATION: "Orientation",
        TAG_SAMPLES_PER_PIXEL: "SamplesPerPixel",
        TAG_ROWS_PER_STRIP: "RowsPerStrip",
        TAG_STRIP_BYTE_COUNTS: "StripByteCounts",
        TAG_X_RESOLUTION: "XResolution",
        TAG_Y_RESOLUTION: "YResolution",
        TAG_PLANAR_CONFIG: "PlanarConfiguration",
        TAG_RESOLUTION_UNIT: "ResolutionUnit",
    }.get(tag, f"tag{tag}")


def _decode_page(data: bytes, tags: dict, page_no: int) -> Page:
    # ---- 尺寸：SHORT 或 LONG 单值 ----
    def dimension(tag: int) -> int:
        if tag not in tags:
            raise TiffError(f"第 {page_no} 页缺少必要结构标签 {_tag_name(tag)}")
        entry_type, count, values = tags[tag]
        if count != 1 or entry_type not in (3, 4):
            raise TiffError(f"第 {page_no} 页 {_tag_name(tag)} 标签类型无效")
        value = values[0]
        if not (MIN_SIDE <= value <= MAX_SIDE):
            raise TiffError(
                f"第 {page_no} 页 {_tag_name(tag)}={value} 超出允许范围（{MIN_SIDE}～{MAX_SIDE} 像素）"
            )
        return value

    width = dimension(TAG_IMAGE_WIDTH)
    length = dimension(TAG_IMAGE_LENGTH)

    # ---- 8 位灰度 ----
    if TAG_BITS_PER_SAMPLE not in tags:
        raise TiffError(f"第 {page_no} 页缺少必要结构标签 BitsPerSample")
    bps_type, bps_count, bps = tags[TAG_BITS_PER_SAMPLE]
    if bps_type != 3 or bps_count != 1 or bps[0] != 8:
        raise TiffError(f"第 {page_no} 页仅支持 8 位灰度（BitsPerSample 必须为单个 SHORT=8）")

    spp = _single_short(tags, TAG_SAMPLES_PER_PIXEL, page_no, default=1)
    if spp != 1:
        raise TiffError(f"第 {page_no} 页仅支持单样本灰度图（SamplesPerPixel 必须为 1）")

    photometric = _single_short(tags, TAG_PHOTOMETRIC, page_no, default=None)
    if photometric is None:
        raise TiffError(f"第 {page_no} 页缺少必要结构标签 PhotometricInterpretation")
    if photometric not in (0, 1):  # 0=WhiteIsZero, 1=BlackIsZero，均为灰度
        raise TiffError(f"第 {page_no} 页 PhotometricInterpretation={photometric}，不是灰度图")

    compression = _single_short(tags, TAG_COMPRESSION, page_no, default=1)
    if compression != 1:
        raise TiffError(f"第 {page_no} 页 Compression={compression}，仅支持未压缩（=1）影像")

    planar = _single_short(tags, TAG_PLANAR_CONFIG, page_no, default=1)
    if planar != 1:
        raise TiffError(f"第 {page_no} 页 PlanarConfiguration={planar}，仅支持单一平面（=1）")

    # ---- 方向：仅默认 ----
    if TAG_ORIENTATION in tags:
        orientation = _single_short(tags, TAG_ORIENTATION, page_no, default=None)
        if orientation != 1:
            raise TiffError(
                f"第 {page_no} 页 Orientation={orientation} 为非默认方向，净化不改变像素几何，拒绝处理"
            )

    # ---- 条带与像素解码 ----
    if TAG_STRIP_OFFSETS not in tags or TAG_STRIP_BYTE_COUNTS not in tags:
        raise TiffError(f"第 {page_no} 页缺少必要结构标签 StripOffsets/StripByteCounts")
    off_type, off_count, offsets = tags[TAG_STRIP_OFFSETS]
    cnt_type, cnt_count, byte_counts = tags[TAG_STRIP_BYTE_COUNTS]
    if off_type not in (3, 4) or cnt_type not in (3, 4) or off_count != cnt_count or off_count < 1:
        raise TiffError(f"第 {page_no} 页条带偏移/字节计数标签无效")

    rows_per_strip = length
    if TAG_ROWS_PER_STRIP in tags:
        rps_type, rps_count, rps_values = tags[TAG_ROWS_PER_STRIP]
        if rps_count != 1 or rps_type not in (3, 4):
            raise TiffError(f"第 {page_no} 页 RowsPerStrip 标签类型无效")
        rows_per_strip = rps_values[0]
    if rows_per_strip <= 0:
        raise TiffError(f"第 {page_no} 页 RowsPerStrip 无效")

    expected_total = width * length
    strips: list[bytes] = []
    for idx, (strip_offset, byte_count) in enumerate(zip(offsets, byte_counts), start=1):
        if strip_offset < 0 or byte_count < 0 or strip_offset + byte_count > len(data):
            raise TiffError(f"第 {page_no} 页第 {idx} 条带数据越界，文件已损坏")
        strips.append(data[strip_offset : strip_offset + byte_count])

    # 逐行核对：除最后一个条带外，每个条带应恰好覆盖整数个完整行
    full_strip_rows = min(rows_per_strip, length)
    full_size = width * full_strip_rows
    last_rows = length - full_strip_rows * (len(strips) - 1)
    if last_rows <= 0 or last_rows > full_strip_rows:
        raise TiffError(f"第 {page_no} 页条带行数与影像高度不一致，文件已损坏")
    expected_sizes = [full_size] * (len(strips) - 1) + [width * last_rows]
    if [len(s) for s in strips] != expected_sizes:
        raise TiffError(f"第 {page_no} 页条带字节数与 {width}×{length} 灰度像素不一致，文件已损坏")

    pixels = b"".join(strips)
    if len(pixels) != expected_total:
        raise TiffError(f"第 {page_no} 页解码像素数异常，文件已损坏")

    page = Page(index=page_no, width=width, length=length, pixels=pixels)

    # ---- 标定标签：要么三枚全有且有效，要么三枚全无 ----
    calibration_present = [
        TAG_X_RESOLUTION in tags,
        TAG_Y_RESOLUTION in tags,
        TAG_RESOLUTION_UNIT in tags,
    ]
    if any(calibration_present) and not all(calibration_present):
        missing = [
            _tag_name(t)
            for t, present in zip(
                (TAG_X_RESOLUTION, TAG_Y_RESOLUTION, TAG_RESOLUTION_UNIT),
                calibration_present,
            )
            if not present
        ]
        raise TiffError(f"第 {page_no} 页标定标签残缺（缺少 {', '.join(missing)}），整件拒绝处理")

    if all(calibration_present):
        for tag in (TAG_X_RESOLUTION, TAG_Y_RESOLUTION):
            entry_type, count, values = tags[tag]
            if entry_type != 5 or count != 1:
                raise TiffError(f"第 {page_no} 页 {_tag_name(tag)} 类型无效（要求单个 RATIONAL）")
            numerator, denominator = values[0]
            if numerator <= 0 or denominator <= 0:
                raise TiffError(f"第 {page_no} 页 {_tag_name(tag)} 数值无效（分子分母必须为正）")
            if tag == TAG_X_RESOLUTION:
                page.x_resolution = (numerator, denominator)
            else:
                page.y_resolution = (numerator, denominator)
        unit = _single_short(tags, TAG_RESOLUTION_UNIT, page_no, default=None)
        if unit not in (1, 2, 3):  # 1=无单位, 2=英寸, 3=厘米
            raise TiffError(f"第 {page_no} 页 ResolutionUnit={unit} 无效（允许 1/2/3）")
        page.resolution_unit = unit
        page.has_calibration = True
    else:
        page.notes.append("本页无任何标定标签（XResolution/YResolution/ResolutionUnit 均缺失），已按无标定输出")

    return page


def parse_tiff(data: bytes) -> list[Page]:
    """解析并逐页校验原件，任何问题都抛 TiffError（不出半成品）。"""
    if not isinstance(data, (bytes, bytearray)) or len(data) < 8:
        raise TiffError("不是有效的 TIFF 文件（长度不足）")
    byte_order = data[:2]
    if byte_order == b"II":
        endian = "<"
    elif byte_order == b"MM":
        endian = ">"
    else:
        raise TiffError("TIFF 字节序魔数无效（要求 II 或 MM）")
    (magic,) = struct.unpack_from(endian + "H", data, 2)
    if magic != 42:
        raise TiffError("不支持的 TIFF 版本（仅支持 Classic TIFF，魔数 42；BigTIFF 请先转换）")
    (first_ifd,) = struct.unpack_from(endian + "I", data, 4)
    if first_ifd < 8:
        raise TiffError("首个 IFD 偏移无效，文件已损坏")

    pages: list[Page] = []
    seen_offsets: set[int] = set()
    offset = first_ifd
    while offset:
        if offset in seen_offsets:
            raise TiffError("IFD 链出现环，文件已损坏")
        seen_offsets.add(offset)
        page_no = len(pages) + 1
        if page_no > MAX_PAGES:
            raise TiffError(f"页数超过上限（最多 {MAX_PAGES} 页）")
        tags, offset = _parse_ifd(data, offset, endian, page_no)
        pages.append(_decode_page(data, tags, page_no))

    if not pages:
        raise TiffError("TIFF 不包含任何影像页")
    return pages


# ---------------------------------------------------------------------- #
# 重建：全新小端 TIFF，只写必要结构标签与有效标定标签
# ---------------------------------------------------------------------- #
# 条目表达：(tag, type, count, 内联 4 字节值 | None, 外部值索引 | None, 条带占位)
@dataclass
class _Entry:
    tag: int
    entry_type: int
    count: int
    inline: bytes | None = None
    external_index: int | None = None
    strip_placeholder: bool = False


def build_sanitized_tiff(data: bytes) -> SanitizedTiff:
    """内存中解密后调用：解析 -> 校验 -> 重建净化 TIFF。

    任何一页失败都会抛 TiffError，不会返回部分结果。输出统一为小端、
    每页单条带的未压缩 8 位灰度 TIFF。
    """
    pages = parse_tiff(data)

    entries_per_page: list[list[_Entry]] = []
    external_blocks: list[bytes] = []  # 所有 >4 字节的值区（RATIONAL）

    for page in pages:
        entries: list[_Entry] = [
            _Entry(TAG_IMAGE_WIDTH, 4, 1, inline=struct.pack("<I", page.width)),
            _Entry(TAG_IMAGE_LENGTH, 4, 1, inline=struct.pack("<I", page.length)),
            _Entry(TAG_BITS_PER_SAMPLE, 3, 1, inline=struct.pack("<H", 8)),
            _Entry(TAG_COMPRESSION, 3, 1, inline=struct.pack("<H", 1)),
            _Entry(TAG_PHOTOMETRIC, 3, 1, inline=struct.pack("<H", 1)),
            _Entry(TAG_STRIP_OFFSETS, 4, 1, strip_placeholder=True),
            _Entry(TAG_SAMPLES_PER_PIXEL, 3, 1, inline=struct.pack("<H", 1)),
            _Entry(TAG_ROWS_PER_STRIP, 4, 1, inline=struct.pack("<I", page.length)),
            _Entry(TAG_STRIP_BYTE_COUNTS, 4, 1, inline=struct.pack("<I", len(page.pixels))),
            _Entry(TAG_PLANAR_CONFIG, 3, 1, inline=struct.pack("<H", 1)),
        ]
        if page.has_calibration:
            x_idx = len(external_blocks)
            external_blocks.append(struct.pack("<II", *page.x_resolution))
            y_idx = len(external_blocks)
            external_blocks.append(struct.pack("<II", *page.y_resolution))
            entries.append(_Entry(TAG_X_RESOLUTION, 5, 1, external_index=x_idx))
            entries.append(_Entry(TAG_Y_RESOLUTION, 5, 1, external_index=y_idx))
            entries.append(
                _Entry(TAG_RESOLUTION_UNIT, 3, 1, inline=struct.pack("<H", page.resolution_unit))
            )
        entries.sort(key=lambda e: e.tag)
        entries_per_page.append(entries)

    # ---- 布局：header -> IFD 链 -> 值区（WORD 对齐）-> 像素条带（WORD 对齐）----
    cursor = 8
    ifd_offsets: list[int] = []
    for entries in entries_per_page:
        ifd_offsets.append(cursor)
        cursor += 2 + 12 * len(entries) + 4

    value_offsets: list[int] = []
    for blob in external_blocks:
        value_offsets.append(cursor)
        cursor += len(blob)
        if cursor % 2:
            cursor += 1  # 值区 WORD 对齐

    strip_offsets: list[int] = []
    for page in pages:
        strip_offsets.append(cursor)
        cursor += len(page.pixels)
        if cursor % 2:
            cursor += 1

    out = bytearray()
    out += b"II\x2a\x00" + struct.pack("<I", ifd_offsets[0])

    for page_idx, entries in enumerate(entries_per_page):
        out += struct.pack("<H", len(entries))
        for entry in entries:
            if entry.strip_placeholder:
                value_field = struct.pack("<I", strip_offsets[page_idx])
            elif entry.external_index is not None:
                value_field = struct.pack("<I", value_offsets[entry.external_index])
            else:
                value_field = entry.inline.ljust(4, b"\x00")  # type: ignore[union-attr]
            out += struct.pack("<HHI", entry.tag, entry.entry_type, entry.count) + value_field
        next_ifd = ifd_offsets[page_idx + 1] if page_idx + 1 < len(ifd_offsets) else 0
        out += struct.pack("<I", next_ifd)

    for idx, blob in enumerate(external_blocks):
        if len(out) < value_offsets[idx]:
            out += b"\x00" * (value_offsets[idx] - len(out))
        out += blob
        if len(out) % 2:
            out += b"\x00"

    for page_idx, page in enumerate(pages):
        if len(out) < strip_offsets[page_idx]:
            out += b"\x00" * (strip_offsets[page_idx] - len(out))
        out += page.pixels
        if len(out) % 2:
            out += b"\x00"

    notes: list[str] = []
    for page in pages:
        if page.notes:
            notes.append(f"第 {page.index} 页：" + "；".join(page.notes))
        else:
            notes.append(f"第 {page.index} 页：标定标签完整有效，已原样保留")
    return SanitizedTiff(content=bytes(out), page_count=len(pages), notes=notes)


def sanitized_output_name(filename: str) -> str:
    """净化件命名：原名去扩展名后加 .sanitized.tif。"""
    lower = filename.lower()
    if lower.endswith(".tiff"):
        stem = filename[:-5]
    elif lower.endswith(".tif"):
        stem = filename[:-4]
    else:
        stem = filename
    return f"{stem}.sanitized.tif"

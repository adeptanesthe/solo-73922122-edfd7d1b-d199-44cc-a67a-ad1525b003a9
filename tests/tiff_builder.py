"""测试辅助：手工构造 Classic TIFF（不依赖第三方图像库）。

仅用于生成可控的合法/非法样本，验证 app/tiff.py 的解析与净化重建。
布局：header -> 每页（IFD 表 -> 该页外部值区，WORD 对齐）-> 像素条带。
外部值一律按标签号索引，条目按标签号排序后偏移仍能正确对应。
"""

from __future__ import annotations

import struct

# 标签号
IMAGE_WIDTH = 256
IMAGE_LENGTH = 257
BITS_PER_SAMPLE = 258
COMPRESSION = 259
PHOTOMETRIC = 262
STRIP_OFFSETS = 273
ORIENTATION = 274
SAMPLES_PER_PIXEL = 277
ROWS_PER_STRIP = 278
STRIP_BYTE_COUNTS = 279
X_RESOLUTION = 282
Y_RESOLUTION = 283
PLANAR_CONFIG = 284
RESOLUTION_UNIT = 296
# “敏感”采集标签与子目录，净化后不得带出
MAKE = 271
SOFTWARE = 305
SUB_IFDS = 330

_TYPE_FMT = {1: ("B", 1), 3: ("H", 2), 4: ("I", 4), 5: None}


class TiffBuilder:
    def __init__(self, endian: str = "<") -> None:
        self.endian = endian
        self.pages: list[dict] = []

    def add_page(
        self,
        width: int,
        length: int,
        pixels: bytes | None = None,
        *,
        calibration: tuple[tuple[int, int], tuple[int, int], int] | None = ((72, 1), (72, 1), 2),
        orientation: int | None = None,
        extra_tags: list[tuple[int, int, object]] | None = None,
        photometric: int = 1,
        compression: int = 1,
        bits_per_sample: int = 8,
        samples_per_pixel: int = 1,
        rows_per_strip: int | None = None,
        planar_config: int | None = None,
    ) -> dict:
        if pixels is None:
            pixels = bytes(((i * 13 + width + length) & 0xFF) for i in range(width * length))
        page = {
            "width": width,
            "length": length,
            "pixels": pixels,
            "calibration": calibration,
            "orientation": orientation,
            "extra_tags": extra_tags or [],
            "photometric": photometric,
            "compression": compression,
            "bits_per_sample": bits_per_sample,
            "samples_per_pixel": samples_per_pixel,
            "rows_per_strip": rows_per_strip,
            "planar_config": planar_config,
        }
        self.pages.append(page)
        return page

    def _encode_value(self, entry_type: int, values: object) -> tuple[int, bytes]:
        """返回 (count, 原始值字节)。"""
        endian = self.endian
        if entry_type == 2:  # ASCII
            raw = values if isinstance(values, bytes) else str(values).encode() + b"\x00"
            return len(raw), raw
        if not isinstance(values, (list, tuple)):
            values = [values]
        if entry_type == 5:  # RATIONAL
            flat: list[int] = []
            for num, den in values:
                flat.extend([num, den])
            return len(values), struct.pack(endian + "I" * len(flat), *flat)
        fmt_char, _unit = _TYPE_FMT[entry_type]
        return len(values), struct.pack(endian + fmt_char * len(values), *values)

    @staticmethod
    def _strip_blobs(page: dict) -> list[bytes]:
        """按 RowsPerStrip 把像素切成多条带（未压缩，每条带整数行）。"""
        width, length = page["width"], page["length"]
        rps = page["rows_per_strip"] or length
        pixels = page["pixels"]
        blobs: list[bytes] = []
        for start in range(0, length, rps):
            rows = min(rps, length - start)
            blobs.append(pixels[start * width : (start + rows) * width])
        return blobs

    def build(self) -> bytes:
        endian = self.endian

        # 每页：tag -> (type, count, 内联 4 字节 | None)；外部值块 tag -> blob
        page_inline: list[dict[int, tuple[int, int, bytes | None]]] = []
        page_external: list[dict[int, bytes]] = []
        for page in self.pages:
            strip_blobs = self._strip_blobs(page)
            inline: dict[int, tuple[int, int, bytes | None]] = {}
            external: dict[int, bytes] = {}

            def add(tag: int, entry_type: int, values: object) -> None:
                count, raw = self._encode_value(entry_type, values)
                if len(raw) <= 4:
                    inline[tag] = (entry_type, count, raw.ljust(4, b"\x00"))
                else:
                    external[tag] = raw
                    inline[tag] = (entry_type, count, None)  # 偏移后填

            add(IMAGE_WIDTH, 4, page["width"])
            add(IMAGE_LENGTH, 4, page["length"])
            add(BITS_PER_SAMPLE, 3, page["bits_per_sample"])
            add(COMPRESSION, 3, page["compression"])
            add(PHOTOMETRIC, 3, page["photometric"])
            add(SAMPLES_PER_PIXEL, 3, page["samples_per_pixel"])
            add(ROWS_PER_STRIP, 4, page["rows_per_strip"] or page["length"])
            if page["orientation"] is not None:
                add(ORIENTATION, 3, page["orientation"])
            if page["planar_config"] is not None:
                add(PLANAR_CONFIG, 3, page["planar_config"])
            if page["calibration"] is not None:
                xres, yres, unit = page["calibration"]
                add(X_RESOLUTION, 5, [xres])
                add(Y_RESOLUTION, 5, [yres])
                add(RESOLUTION_UNIT, 3, unit)
            for tag, entry_type, values in page["extra_tags"]:
                add(tag, entry_type, values)
            add(STRIP_BYTE_COUNTS, 4, [len(s) for s in strip_blobs])
            add(STRIP_OFFSETS, 4, [0] * len(strip_blobs))  # 偏移后填
            page_inline.append(inline)
            page_external.append(external)
            page["_strip_blobs"] = strip_blobs

        # 线性布局：header -> 每页（IFD 表 -> 外部值区，均 WORD 对齐）-> 像素条带。
        cursor = 8
        ifd_offsets: list[int] = []
        external_offset_map: list[dict[int, int]] = []
        for inline, external in zip(page_inline, page_external):
            if cursor % 2:
                cursor += 1
            ifd_offsets.append(cursor)
            cursor += 2 + 12 * len(inline) + 4
            tag_offsets: dict[int, int] = {}
            for tag in sorted(external):  # 值区与条目同为标签号升序
                if cursor % 2:
                    cursor += 1
                tag_offsets[tag] = cursor
                cursor += len(external[tag])
            external_offset_map.append(tag_offsets)
        # 像素条带：每页的多个条带连续、WORD 对齐；记录每条带的绝对偏移
        strip_offset_lists: list[list[int]] = []
        for page in self.pages:
            offsets: list[int] = []
            for blob in page["_strip_blobs"]:
                if cursor % 2:
                    cursor += 1
                offsets.append(cursor)
                cursor += len(blob)
            strip_offset_lists.append(offsets)

        out = bytearray()
        out += b"II" if endian == "<" else b"MM"
        out += struct.pack(endian + "H", 42)
        out += struct.pack(endian + "I", ifd_offsets[0])

        for idx, inline in enumerate(page_inline):
            external = page_external[idx]
            if len(out) < ifd_offsets[idx]:
                out += b"\x00" * (ifd_offsets[idx] - len(out))
            out += struct.pack(endian + "H", len(inline))
            for tag in sorted(inline):
                entry_type, count, value_bytes = inline[tag]
                if value_bytes is None:
                    value_field = struct.pack(endian + "I", external_offset_map[idx][tag])
                elif tag == STRIP_OFFSETS:
                    # 单条带时内联 LONG，直接给绝对偏移；多条带走外部值区回填
                    value_field = struct.pack(endian + "I", strip_offset_lists[idx][0])
                else:
                    value_field = value_bytes
                out += struct.pack(endian + "HHI", tag, entry_type, count) + value_field
            next_ifd = ifd_offsets[idx + 1] if idx + 1 < len(ifd_offsets) else 0
            out += struct.pack(endian + "I", next_ifd)
            for tag in sorted(external):
                target = external_offset_map[idx][tag]
                if len(out) < target:
                    out += b"\x00" * (target - len(out))
                if tag == STRIP_OFFSETS:
                    # 外部条带偏移数组：布局已确定，回填真实绝对偏移
                    out += struct.pack(endian + "I" * len(strip_offset_lists[idx]),
                                       *strip_offset_lists[idx])
                else:
                    out += external[tag]

        # 写入像素条带（可能每页多条带），单条带内联偏移在条目序列化时已直接给出
        for idx, page in enumerate(self.pages):
            for blob, target in zip(page["_strip_blobs"], strip_offset_lists[idx]):
                if len(out) < target:
                    out += b"\x00" * (target - len(out))
                out += blob
        return bytes(out)


def parse_tiff_tags(blob: bytes) -> list[dict[int, tuple[int, int, object]]]:
    """测试用最小解析器：读回每页标签，用于断言净化结果。"""
    endian = "<" if blob[:2] == b"II" else ">"
    (first_ifd,) = struct.unpack_from(endian + "I", blob, 4)
    pages: list[dict[int, tuple[int, int, object]]] = []
    offset = first_ifd
    while offset:
        (count,) = struct.unpack_from(endian + "H", blob, offset)
        tags: dict[int, tuple[int, int, object]] = {}
        for i in range(count):
            entry = blob[offset + 2 + i * 12 : offset + 2 + (i + 1) * 12]
            tag, entry_type, value_count = struct.unpack(endian + "HHI", entry[:8])
            value_field = entry[8:]
            unit = {3: 2, 4: 4}.get(entry_type)
            if entry_type in (3, 4) and unit * value_count > 4:
                # 值超过 4 字节：内联字段存放外部偏移
                (external,) = struct.unpack(endian + "I", value_field)
                fmt = "H" if entry_type == 3 else "I"
                values = list(struct.unpack_from(endian + fmt * value_count, blob, external))
            elif entry_type == 3:
                values = list(struct.unpack(endian + "H" * value_count, value_field[: 2 * value_count]))
            elif entry_type == 4:
                values = list(struct.unpack(endian + "I" * value_count, value_field[: 4 * value_count]))
            elif entry_type == 5:
                (external,) = struct.unpack(endian + "I", value_field)
                nums = struct.unpack_from(endian + "I" * (2 * value_count), blob, external)
                values = [(nums[j], nums[j + 1]) for j in range(0, len(nums), 2)]
            else:
                values = value_field
            tags[tag] = (entry_type, value_count, values)
        (offset,) = struct.unpack_from(endian + "I", blob, offset + 2 + count * 12)
        pages.append(tags)
    return pages

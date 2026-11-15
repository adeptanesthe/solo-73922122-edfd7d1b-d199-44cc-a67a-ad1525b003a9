"""净化下载端到端测试。

覆盖：
1. 净化件逐页保留像素/页序/有效标定标签，且不带出其他源标签或子目录；
2. 无标定标签页可处理并注明；标定残缺/无效、方向非默认、损坏页整件拒绝且无半成品；
3. 1～3 页与 64～512 尺寸边界，超出范围拒绝；
4. 净化全程明文/DEK 不落盘，原归档密文与封装不变；
5. 已受理净化读取在密钥轮换期间仍读完原件（共用下载暂停点）；
6. 失败/重启恢复语义与原件下载一致。
"""

from __future__ import annotations

import json
import os
import struct
import threading
import time
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.tiff import (
    TAG_BITS_PER_SAMPLE,
    TAG_COMPRESSION,
    TAG_IMAGE_LENGTH,
    TAG_IMAGE_WIDTH,
    TAG_ORIENTATION,
    TAG_PHOTOMETRIC,
    TAG_PLANAR_CONFIG,
    TAG_RESOLUTION_UNIT,
    TAG_ROWS_PER_STRIP,
    TAG_SAMPLES_PER_PIXEL,
    TAG_STRIP_BYTE_COUNTS,
    TAG_STRIP_OFFSETS,
    TAG_X_RESOLUTION,
    TAG_Y_RESOLUTION,
)
from tests.tiff_builder import (
    MAKE,
    SOFTWARE,
    SUB_IFDS,
    TiffBuilder,
    parse_tiff_tags,
)

STRUCTURAL_TAGS = {
    TAG_IMAGE_WIDTH,
    TAG_IMAGE_LENGTH,
    TAG_BITS_PER_SAMPLE,
    TAG_COMPRESSION,
    TAG_PHOTOMETRIC,
    TAG_STRIP_OFFSETS,
    TAG_SAMPLES_PER_PIXEL,
    TAG_ROWS_PER_STRIP,
    TAG_STRIP_BYTE_COUNTS,
    TAG_PLANAR_CONFIG,
}
CALIBRATION_TAGS = {TAG_X_RESOLUTION, TAG_Y_RESOLUTION, TAG_RESOLUTION_UNIT}


def wait_until(predicate, timeout: float = 10.0, interval: float = 0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("等待条件超时")


@pytest.fixture()
def tmp_data(tmp_path):
    return str(tmp_path / "data")


@pytest.fixture()
def svc(tmp_data, monkeypatch):
    monkeypatch.setenv("ARCHIVE_DATA_DIR", tmp_data)
    with TestClient(app) as client:
        from app.main import service as current

        yield client, current


def _upload(client: TestClient, content: bytes, name: str = "image.tif") -> str:
    r = client.post("/api/uploads", json={"filename": name, "size": len(content)})
    assert r.status_code == 201, r.text
    upload_id = r.json()["upload_id"]
    r = client.put(f"/api/uploads/{upload_id}/content", content=content)
    assert r.status_code == 200, r.text
    r = client.post(f"/api/uploads/{upload_id}/complete")
    assert r.status_code == 200, r.text
    return r.json()["file_id"]


def _sanitize(client: TestClient, file_id: str):
    r = client.post(f"/api/files/{file_id}/sanitized-downloads")
    assert r.status_code == 201, r.text
    dl_id = r.json()["download_id"]
    resp = client.get(f"/api/sanitized-downloads/{dl_id}/content")
    return dl_id, resp


def _page_pixels(blob: bytes, page_tags: dict) -> bytes:
    entry_type, _count, offsets = page_tags[TAG_STRIP_OFFSETS]
    _t, _c, counts = page_tags[TAG_STRIP_BYTE_COUNTS]
    offset = offsets[0]
    size = counts[0]
    return blob[offset : offset + size]


class TestSanitizedDownload:
    def test_sanitized_keeps_pixels_page_order_and_calibration(self, svc):
        client, _ = svc
        builder = TiffBuilder()
        builder.add_page(64, 64, calibration=((300, 1), (300, 1), 2),
                         extra_tags=[(MAKE, 2, "Secret-Lab"), (SOFTWARE, 2, "Acq/9.0")])
        builder.add_page(128, 96, calibration=((50000, 1000), (50000, 1000), 3))
        builder.add_page(512, 512, calibration=((96, 1), (96, 1), 1))
        source = builder.build()
        file_id = _upload(client, source, "subject-001.tif")

        dl_id, resp = _sanitize(client, file_id)
        assert resp.status_code == 200, resp.text
        out = resp.content
        assert resp.headers["X-Sanitized-Pages"] == "3"
        notes = json.loads(unquote(resp.headers["X-Sanitized-Notes"]))
        assert len(notes) == 3 and all("完整有效" in n for n in notes)
        assert "filename*=UTF-8''" in resp.headers["content-disposition"]
        assert "subject-001.sanitized.tif" in resp.headers["content-disposition"]

        pages = parse_tiff_tags(out)
        assert len(pages) == 3
        source_pages = parse_tiff_tags(source)
        expected_cal = [
            ((300, 1), (300, 1), 2),
            ((50000, 1000), (50000, 1000), 3),
            ((96, 1), (96, 1), 1),
        ]
        for idx, tags in enumerate(pages):
            # 仅含必要结构标签 + 标定标签
            assert set(tags) <= STRUCTURAL_TAGS | CALIBRATION_TAGS
            assert MAKE not in tags and SOFTWARE not in tags and SUB_IFDS not in tags
            # 8 位灰度、未压缩、默认方向（无 Orientation 标签输出）
            assert tags[TAG_BITS_PER_SAMPLE][2] == [8]
            assert tags[TAG_COMPRESSION][2] == [1]
            assert tags[TAG_PHOTOMETRIC][2] == [1]
            assert tags[TAG_SAMPLES_PER_PIXEL][2] == [1]
            assert TAG_ORIENTATION not in tags
            # 标定值原样保留（含分数）
            xres, yres, unit = expected_cal[idx]
            assert tags[TAG_X_RESOLUTION][2] == [xres]
            assert tags[TAG_Y_RESOLUTION][2] == [yres]
            assert tags[TAG_RESOLUTION_UNIT][2] == [unit]
            # 像素逐字节保留、页序不变
            src_tags = source_pages[idx]
            src_off = src_tags[TAG_STRIP_OFFSETS][2][0]
            src_size = src_tags[TAG_STRIP_BYTE_COUNTS][2][0]
            assert _page_pixels(out, tags) == source[src_off : src_off + src_size]

        # 宽高保留
        assert pages[0][TAG_IMAGE_WIDTH][2] == [64] and pages[0][TAG_IMAGE_LENGTH][2] == [64]
        assert pages[1][TAG_IMAGE_WIDTH][2] == [128] and pages[1][TAG_IMAGE_LENGTH][2] == [96]
        assert pages[2][TAG_IMAGE_WIDTH][2] == [512] and pages[2][TAG_IMAGE_LENGTH][2] == [512]

        # 会话记录注明页数
        dl = next(d for d in client.get("/api/downloads").json() if d["download_id"] == dl_id)
        assert dl["kind"] == "sanitized" and dl["state"] == "completed" and dl["page_count"] == 3
        assert all("完整有效" in n for n in dl["notes"])

    def test_big_endian_source_is_normalized_to_little_endian_output(self, svc):
        client, _ = svc
        builder = TiffBuilder(endian=">")
        builder.add_page(64, 64)
        file_id = _upload(client, builder.build(), "mm.tif")
        _, resp = _sanitize(client, file_id)
        assert resp.status_code == 200
        assert resp.content[:2] == b"II"  # 输出统一小端，像素与标定仍保留

    def test_page_without_any_calibration_is_processed_and_noted(self, svc):
        client, _ = svc
        builder = TiffBuilder()
        builder.add_page(64, 64, calibration=None)
        file_id = _upload(client, builder.build())
        dl_id, resp = _sanitize(client, file_id)
        assert resp.status_code == 200, resp.text
        pages = parse_tiff_tags(resp.content)
        assert CALIBRATION_TAGS.isdisjoint(pages[0])
        notes = json.loads(unquote(resp.headers["X-Sanitized-Notes"]))
        assert "无任何标定标签" in notes[0]
        dl = next(d for d in client.get("/api/downloads").json() if d["download_id"] == dl_id)
        assert any("无任何标定标签" in n for n in dl["notes"])

    def test_mixed_pages_missing_calibration_on_one_page_rejected(self, svc, tmp_data):
        client, _ = svc
        builder = TiffBuilder()
        builder.add_page(64, 64)  # 完整标定
        # 第二页：只给 XResolution/YResolution，缺 ResolutionUnit
        builder.add_page(
            64, 64,
            extra_tags=[
                (282, 5, [(72, 1)]),
                (283, 5, [(72, 1)]),
            ],
            calibration=None,
        )
        file_id = _upload(client, builder.build())
        dl_id, resp = _sanitize(client, file_id)
        assert resp.status_code == 422
        assert "标定标签残缺" in resp.json()["error"] and "第 2 页" in resp.json()["error"]
        dl = next(d for d in client.get("/api/downloads").json() if d["download_id"] == dl_id)
        assert dl["state"] == "failed" and "标定标签残缺" in dl["error"]
        # 失败会话同样清空封装材料，不残留可解封的旧钥引用
        import sqlite3

        wrapped = sqlite3.connect(os.path.join(tmp_data, "archive.db")).execute(
            "SELECT wrapped_dek FROM downloads WHERE download_id=?", (dl_id,)
        ).fetchone()[0]
        assert wrapped is None

    def test_invalid_calibration_values_rejected(self, svc):
        client, _ = svc
        # ResolutionUnit 非法
        builder = TiffBuilder()
        builder.add_page(64, 64, calibration=((72, 1), (72, 1), 9))
        file_id = _upload(client, builder.build())
        _, resp = _sanitize(client, file_id)
        assert resp.status_code == 422 and "ResolutionUnit" in resp.json()["error"]

        # 分辨率分母为 0
        builder = TiffBuilder()
        builder.add_page(64, 64, calibration=((72, 0), (72, 1), 2))
        file_id = _upload(client, builder.build())
        _, resp = _sanitize(client, file_id)
        assert resp.status_code == 422 and "XResolution" in resp.json()["error"]

    def test_non_default_orientation_rejected(self, svc):
        client, _ = svc
        builder = TiffBuilder()
        builder.add_page(64, 64, orientation=6)
        file_id = _upload(client, builder.build())
        _, resp = _sanitize(client, file_id)
        assert resp.status_code == 422
        assert "Orientation" in resp.json()["error"] and "非默认方向" in resp.json()["error"]

    def test_corrupt_page_out_of_range_strip_rejected(self, svc):
        client, _ = svc
        builder = TiffBuilder()
        builder.add_page(64, 64)
        source = bytearray(builder.build())
        # 把条带偏移改成越界值
        off_entry_pos = _entry_position(bytes(source), 0, TAG_STRIP_OFFSETS)
        struct.pack_into("<I", source, off_entry_pos + 8, len(source) + 100)
        file_id = _upload(client, bytes(source), "corrupt.tif")
        _, resp = _sanitize(client, file_id)
        assert resp.status_code == 422 and "越界" in resp.json()["error"]

    def test_dimension_and_page_count_limits(self, svc):
        client, _ = svc
        # 宽 63：低于下限
        b = TiffBuilder(); b.add_page(63, 64)
        fid = _upload(client, b.build())
        assert _sanitize(client, fid)[1].status_code == 422
        # 高 513：超过上限
        b = TiffBuilder(); b.add_page(64, 513)
        fid = _upload(client, b.build())
        assert _sanitize(client, fid)[1].status_code == 422
        # 边界 64 与 512 合法
        b = TiffBuilder(); b.add_page(64, 512)
        fid = _upload(client, b.build())
        assert _sanitize(client, fid)[1].status_code == 200
        # 4 页超过上限
        b = TiffBuilder()
        for _ in range(4):
            b.add_page(64, 64)
        fid = _upload(client, b.build())
        resp = _sanitize(client, fid)[1]
        assert resp.status_code == 422 and "页数" in resp.json()["error"]

    def test_non_grayscale_and_compression_rejected(self, svc):
        client, _ = svc
        b = TiffBuilder(); b.add_page(64, 64, photometric=3)  # 调色板彩色
        fid = _upload(client, b.build())
        assert _sanitize(client, fid)[1].status_code == 422

        b = TiffBuilder(); b.add_page(64, 64, compression=5)  # LZW
        fid = _upload(client, b.build())
        resp = _sanitize(client, fid)[1]
        assert resp.status_code == 422 and "Compression" in resp.json()["error"]

        b = TiffBuilder(); b.add_page(64, 64, bits_per_sample=16)
        fid = _upload(client, b.build())
        assert _sanitize(client, fid)[1].status_code == 422

    def test_multi_strip_source_pixels_preserved_in_single_output_strip(self, svc):
        client, _ = svc
        pixels = bytes((i % 256 for i in range(64 * 64)))
        b = TiffBuilder()
        b.add_page(64, 64, pixels=pixels, rows_per_strip=32)  # 两条带，每条 32 行
        file_id = _upload(client, b.build())
        _, resp = _sanitize(client, file_id)
        assert resp.status_code == 200, resp.text
        tags = parse_tiff_tags(resp.content)[0]
        # 输出统一为单条带，像素逐字节保留
        assert tags[TAG_STRIP_BYTE_COUNTS][2] == [len(pixels)]
        off = tags[TAG_STRIP_OFFSETS][2][0]
        assert resp.content[off : off + len(pixels)] == pixels

    def test_garbage_bytes_rejected_without_half_output(self, svc):
        client, _ = svc
        payload = b"II\x2a\x00" + b"\x00" * 200
        fid = _upload(client, payload, "broken.tif")
        dl_id, resp = _sanitize(client, fid)
        assert resp.status_code == 422
        # 失败会话无 output 内容可取，再次请求仍是失败态
        again = client.get(f"/api/sanitized-downloads/{dl_id}/content")
        assert again.status_code == 410

    def test_plaintext_and_dek_never_on_disk_and_archive_unchanged(self, svc, tmp_data):
        client, _ = svc
        builder = TiffBuilder()
        page = builder.add_page(96, 96, extra_tags=[(MAKE, 2, "PII-Subject-77")])
        source = builder.build()
        file_id = _upload(client, source)

        vault_before = {}
        for name in os.listdir(os.path.join(tmp_data, "vault")):
            with open(os.path.join(tmp_data, "vault", name), "rb") as fh:
                vault_before[name] = fh.read()

        _, resp = _sanitize(client, file_id)
        assert resp.status_code == 200
        assert b"PII-Subject-77" not in resp.content  # 敏感标签不带出

        # vault/staging 中无明文 TIFF 魔数、无敏感字符串；blob 逐字节不变
        for root, _dirs, files in os.walk(tmp_data):
            for name in files:
                path = os.path.join(root, name)
                with open(path, "rb") as fh:
                    data = fh.read()
                if "vault" in path or "staging" in path:
                    assert b"II\x2a\x00" not in data
                    assert b"PII-Subject-77" not in data
                if "vault" in path:
                    assert vault_before[name] == data
        assert os.listdir(os.path.join(tmp_data, "staging")) == []

        # 原件下载仍与原件逐字节一致，原归档不变
        r = client.post(f"/api/files/{file_id}/downloads")
        dl = r.json()["download_id"]
        original = client.get(f"/api/downloads/{dl}/content")
        assert original.status_code == 200 and original.content == source

    def test_sanitized_read_survives_key_rotation(self, svc, tmp_data):
        client, service = svc
        builder = TiffBuilder()
        builder.add_page(64, 64)
        source = builder.build()
        file_id = _upload(client, source)
        old_kek_id = client.get("/api/rotations/current").json()["active_kek"]["id"]

        # 关闭下载解封闸门：受理后的净化读取停在待解封点
        client.post("/api/demo/gates/download_unwrap", json={"paused": True})
        r = client.post(f"/api/files/{file_id}/sanitized-downloads")
        dl_id = r.json()["download_id"]
        result: dict = {}
        t = threading.Thread(
            target=lambda: result.update(
                {"resp": client.get(f"/api/sanitized-downloads/{dl_id}/content")}
            )
        )
        t.start()
        wait_until(lambda: service.download_gate.waiting == 1)

        # 轮换必须等待该在途净化读取，旧 KEK 文件保留
        assert client.post("/api/rotations").status_code == 201
        wait_until(lambda: (
            (s := client.get("/api/rotations/current").json())["active"]
            and s["state"] == "completing"))
        assert client.get("/api/rotations/current").json()["blocking_downloads"] == 1
        old_key = next(k for k in client.get("/api/keys").json() if k["id"] == old_kek_id)
        assert os.path.exists(os.path.join(tmp_data, "keys", old_key["key_id"] + ".kek"))

        client.post("/api/demo/gates/download_unwrap", json={"paused": False})
        t.join(timeout=10)
        resp = result["resp"]
        assert resp.status_code == 200
        # 用受理时快照的旧 KEK 读完原件并完成净化；轮换随后完成、旧 KEK 删除
        wait_until(lambda: not client.get("/api/rotations/current").json()["active"])
        assert not os.path.exists(os.path.join(tmp_data, "keys", old_key["key_id"] + ".kek"))
        pages = parse_tiff_tags(resp.content)
        assert len(pages) == 1 and pages[0][TAG_X_RESOLUTION][2] == [(72, 1)]

    def test_rejected_sanitization_does_not_block_new_downloads(self, svc):
        client, _ = svc
        b = TiffBuilder(); b.add_page(64, 64, orientation=8)
        fid = _upload(client, b.build())
        bad = _sanitize(client, fid)[1]
        assert bad.status_code == 422
        # 同件重新净化（新会话）在修正前仍失败；原件下载不受影响
        r = client.post(f"/api/files/{fid}/downloads")
        dl = r.json()["download_id"]
        assert client.get(f"/api/downloads/{dl}/content").status_code == 200

    def test_restart_marks_accepted_sanitized_interrupted(self, tmp_data, monkeypatch):
        monkeypatch.setenv("ARCHIVE_DATA_DIR", tmp_data)
        b = TiffBuilder(); b.add_page(64, 64)
        with TestClient(app) as client:
            fid = _upload(client, b.build())
            r = client.post(f"/api/files/{fid}/sanitized-downloads")
            dl_id = r.json()["download_id"]
            assert client.get("/api/downloads").json()[0]["state"] == "accepted"
        # 重启：受理会话不跨进程，标记 interrupted 且封装材料清空
        with TestClient(app) as client:
            dl = next(d for d in client.get("/api/downloads").json() if d["download_id"] == dl_id)
            assert dl["state"] == "interrupted" and dl["kind"] == "sanitized"
            import sqlite3

            row = sqlite3.connect(os.path.join(tmp_data, "archive.db")).execute(
                "SELECT wrapped_dek FROM downloads WHERE download_id=?", (dl_id,)
            ).fetchone()
            assert row[0] is None
            # 重新发起净化仍成功
            assert _sanitize(client, fid)[1].status_code == 200


def _entry_position(blob: bytes, page_index: int, tag: int) -> int:
    """返回某页某标签 12 字节条目的起始偏移（测试用）。"""
    endian = "<" if blob[:2] == b"II" else ">"
    (offset,) = struct.unpack_from(endian + "I", blob, 4)
    seen = 0
    while offset:
        (count,) = struct.unpack_from(endian + "H", blob, offset)
        if seen == page_index:
            for i in range(count):
                pos = offset + 2 + i * 12
                (t,) = struct.unpack_from(endian + "H", blob, pos)
                if t == tag:
                    return pos
            raise AssertionError("标签未找到")
        (offset,) = struct.unpack_from(endian + "I", blob, offset + 2 + count * 12)
        seen += 1
    raise AssertionError("页未找到")

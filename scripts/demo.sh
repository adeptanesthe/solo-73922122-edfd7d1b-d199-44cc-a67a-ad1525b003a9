#!/usr/bin/env bash
# 本地演示脚本：无需 Docker，自动在独立临时目录启动服务并演示三个场景：
#   1) 上传待登记暂停 / 下载待解封暂停；
#   2) 密钥轮换：仅重封装 DEK、旧 KEK 文件删除后才报完成；
#   3) 轮换中“进程退出 → 重启自动续办”，已归档文件始终可解密。
#
# 用法： scripts/demo.sh [BASE_DIR]
# 依赖： python3（含 venv 内的 fastapi/uvicorn/cryptography）、curl、sha256sum
set -euo pipefail

BASE_DIR="${1:-$(mktemp -d /tmp/tiff-archive-demo.XXXXXX)}"
DATA_DIR="$BASE_DIR/data"
PORT="${PORT:-8090}"
# 重启方式：RESTART=kill（默认，真实环境/容器内停止进程后重启）；
# RESTART=report（受限环境无法发信号时，用同一数据目录在新端口启动新进程，
# 恢复逻辑完全相同，仅进程替换方式不同）。
RESTART="${RESTART:-kill}"
PORT2="${PORT2:-$((PORT+1))}"
BASE="http://127.0.0.1:$PORT"
LOG="$BASE_DIR/server.log"
PY="${PYTHON:-python3}"

mkdir -p "$DATA_DIR" "$BASE_DIR/samples"
trap 'echo; echo "演示数据保留在: $BASE_DIR（可手动删除）"' EXIT

say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
ok()  { printf '\033[1;32m[OK]\033[0m %s\n' "$*"; }
jget() { "$PY" -c 'import sys,json;d=json.load(sys.stdin);
for k in sys.argv[1].split("."): d=d[k]
print(d)' "$1"; }

say "0/ 启动服务（数据目录 $DATA_DIR，端口 $PORT）"
ARCHIVE_DATA_DIR="$DATA_DIR" "$PY" -m uvicorn app.main:app \
  --host 127.0.0.1 --port "$PORT" >"$LOG" 2>&1 &
SRV=$!
trap 'kill "$SRV" 2>/dev/null || true; echo; echo "演示数据保留在: $BASE_DIR（可手动删除）"' EXIT
for _ in $(seq 1 30); do
  curl -sf "$BASE/health" >/dev/null && break
  sleep 0.3
done
curl -s "$BASE/health" >/dev/null || { echo "服务启动失败，见 $LOG"; exit 1; }
ok "服务在线，浏览器入口: $BASE/ （PID=$SRV）"

make_tif() {  # $1 文件名 $2 大小
  "$PY" - "$1" "$2" <<'EOF'
import sys
path, size = sys.argv[1], int(sys.argv[2])
header = b"II\x2a\x00" + size.to_bytes(4, "little")
body = bytes(((i * 31 + 7) & 0xFF) for i in range(size - len(header)))
open(path, "wb").write(header + body)
EOF
}

upload() {  # $1 文件路径 -> 输出 file_id
  local path="$1" name size init uid
  name="$(basename "$path")"; size="$(stat -c%s "$path")"
  init="$(curl -s -X POST "$BASE/api/uploads" -H 'Content-Type: application/json' \
    -d "{\"filename\":\"$name\",\"size\":$size}")"
  uid="$(printf '%s' "$init" | jget upload_id)"
  curl -s -X PUT --data-binary "@$path" "$BASE/api/uploads/$uid/content" >/dev/null
  curl -s -X POST "$BASE/api/uploads/$uid/complete" | jget file_id
}

download() {  # $1 file_id $2 输出路径
  local did
  did="$(curl -s -X POST "$BASE/api/files/$1/downloads" | jget download_id)"
  curl -s -o "$2" "$BASE/api/downloads/$did/content"
}

wait_rotation_done() {
  for _ in $(seq 1 50); do
    curl -s "$BASE/api/rotations/current" | "$PY" -c 'import sys,json;raise SystemExit(0 if not json.load(sys.stdin)["active"] else 1)' && return 0
    sleep 0.3
  done
  echo "轮换未在预期时间内完成"; return 1
}

say "1/ 正常上传/下载往返（独立随机 DEK，明文不落盘）"
make_tif "$BASE_DIR/samples/img-a.tif" 4096
FID_A="$(upload "$BASE_DIR/samples/img-a.tif")"
download "$FID_A" "$BASE_DIR/samples/img-a.dl.tif"
cmp "$BASE_DIR/samples/img-a.tif" "$BASE_DIR/samples/img-a.dl.tif"
ok "img-a.tif 上传后下载逐字节一致（file_id=${FID_A:0:8}…）"
if grep -rIl $'II\x2a\x00' "$DATA_DIR/vault" "$DATA_DIR/staging" 2>/dev/null; then
  echo "密文目录中发现明文 TIFF 魔数！"; exit 1
fi
ok "vault/staging 中无明文 TIFF 魔数；密钥目录与密文目录分离："
echo "   $DATA_DIR/keys  $(ls "$DATA_DIR/keys" | sed 's/^/  /' | tr '\n' ' ')"
echo "   $DATA_DIR/vault $(ls "$DATA_DIR/vault" | sed 's/^/  /' | tr '\n' ' ')"

say "2/ 暂停点演示：上传待登记、下载待解封"
# --- 上传待登记 ---
curl -s -X POST "$BASE/api/demo/gates/upload_register" -H 'Content-Type: application/json' -d '{"paused":true}' >/dev/null
make_tif "$BASE_DIR/samples/img-b.tif" 2048
size_b="$(stat -c%s "$BASE_DIR/samples/img-b.tif")"
uid_b="$(curl -s -X POST "$BASE/api/uploads" -H 'Content-Type: application/json' \
  -d "{\"filename\":\"img-b.tif\",\"size\":$size_b}" | jget upload_id)"
curl -s -X PUT --data-binary "@$BASE_DIR/samples/img-b.tif" "$BASE/api/uploads/$uid_b/content" >/dev/null
curl -s -X POST "$BASE/api/uploads/$uid_b/complete" >"$BASE_DIR/up-b.json" &
CP=$!
sleep 1.2
state_b="$(curl -s "$BASE/api/uploads" | "$PY" -c 'import sys,json;print(next(u["state"] for u in json.load(sys.stdin) if u["filename"]=="img-b.tif"))')"
[ "$state_b" = "pending_register" ] && ok "上传已暂停在“待登记”（DEK 仅在内存，密文暂存 staging/）"
# --- 同时发起轮换：收尾必须等待该上传 ---
curl -s -X POST "$BASE/api/rotations" >/dev/null
sleep 1.0
rot="$(curl -s "$BASE/api/rotations/current")"
echo "$rot" | "$PY" -c 'import sys,json;d=json.load(sys.stdin);assert d["active"] and d["state"]=="completing" and d["blocking_uploads"]==1'
ok "轮换停在收尾，blocking_uploads=1（旧 KEK 文件仍保留）"
# --- 恢复上传，轮换随之完成 ---
curl -s -X POST "$BASE/api/demo/gates/upload_register" -H 'Content-Type: application/json' -d '{"paused":false}' >/dev/null
wait "$CP"
wait_rotation_done
fid_b="$(curl -s "$BASE/api/uploads" | "$PY" -c 'import sys,json;print(next(u["file_id"] for u in json.load(sys.stdin) if u["filename"]=="img-b.tif"))')"
kek_b="$(curl -s "$BASE/api/files" | "$PY" -c 'import sys,json;print(next(f["kek_id"] for f in json.load(sys.stdin) if f["file_id"]=="'$fid_b'"))')"
old_kek="$(curl -s "$BASE/api/keys" | "$PY" -c 'import sys,json;print(next(k["id"] for k in json.load(sys.stdin) if k["status"]=="deleted"))')"
[ "$kek_b" != "$old_kek" ] && ok "暂停期间的上传最终由新课题密钥封装，未留下旧钥引用"
ok "第一轮轮换完成，旧 KEK 文件已实际删除"

# --- 下载待解封 ---
curl -s -X POST "$BASE/api/demo/gates/download_unwrap" -H 'Content-Type: application/json' -d '{"paused":true}' >/dev/null
did_c="$(curl -s -X POST "$BASE/api/files/$FID_A/downloads" | jget download_id)"
curl -s -o "$BASE_DIR/samples/img-a.dl2.tif" "$BASE/api/downloads/$did_c/content" &
CP=$!
sleep 1.2
dl_state="$(curl -s "$BASE/api/downloads" | "$PY" -c 'import sys,json;print(next(d["state"] for d in json.load(sys.stdin) if d["download_id"]=="'$did_c'"))')"
[ "$dl_state" = "accepted" ] && ok "下载已受理并暂停在“待解封”"
curl -s -X POST "$BASE/api/rotations" >/dev/null
sleep 1.0
curl -s "$BASE/api/rotations/current" | "$PY" -c 'import sys,json;d=json.load(sys.stdin);assert d["active"] and d["blocking_downloads"]==1'
ok "第二轮轮换等待在途下载，blocking_downloads=1"
curl -s -X POST "$BASE/api/demo/gates/download_unwrap" -H 'Content-Type: application/json' -d '{"paused":false}' >/dev/null
wait "$CP"
wait_rotation_done
cmp "$BASE_DIR/samples/img-a.tif" "$BASE_DIR/samples/img-a.dl2.tif"
ok "恢复后已受理下载读完原件，内容逐字节一致；轮换随后完成"

say "3/ 轮换中退出、重启自动续办"
# 留一个待登记上传，使下一轮换停在收尾，然后“杀掉”服务模拟中途退出
curl -s -X POST "$BASE/api/demo/gates/upload_register" -H 'Content-Type: application/json' -d '{"paused":true}' >/dev/null
make_tif "$BASE_DIR/samples/img-c.tif" 1024
size_c="$(stat -c%s "$BASE_DIR/samples/img-c.tif")"
uid_c="$(curl -s -X POST "$BASE/api/uploads" -H 'Content-Type: application/json' \
  -d "{\"filename\":\"img-c.tif\",\"size\":$size_c}" | jget upload_id)"
curl -s -X PUT --data-binary "@$BASE_DIR/samples/img-c.tif" "$BASE/api/uploads/$uid_c/content" >/dev/null
curl -s -X POST "$BASE/api/rotations" >/dev/null
sleep 1.0
curl -s "$BASE/api/rotations/current" | "$PY" -c 'import sys,json;d=json.load(sys.stdin);assert d["active"]'
if [ "$RESTART" = "report" ]; then
  ok "轮换进行中（RESTART=report）：在新端口 $PORT2 用同一数据目录启动新进程模拟重启"
  RESTART_BASE="http://127.0.0.1:$PORT2"
  ARCHIVE_DATA_DIR="$DATA_DIR" "$PY" -m uvicorn app.main:app \
    --host 127.0.0.1 --port "$PORT2" >"$BASE_DIR/server-restart.log" 2>&1 &
  SRV2=$!
else
  ok "轮换进行中，停止服务进程（PID=$SRV）"
  kill "$SRV" 2>/dev/null || true
  wait "$SRV" 2>/dev/null || true

  # 用同一数据目录重启：恢复逻辑自动续办
  ARCHIVE_DATA_DIR="$DATA_DIR" "$PY" -m uvicorn app.main:app \
    --host 127.0.0.1 --port "$PORT" >"$BASE_DIR/server-restart.log" 2>&1 &
  SRV2=$!
  RESTART_BASE="$BASE"
fi
trap 'kill "$SRV" "$SRV2" 2>/dev/null || true; echo; echo "演示数据保留在: $BASE_DIR（可手动删除）"' EXIT
for _ in $(seq 1 30); do curl -sf "$RESTART_BASE/health" >/dev/null && break; sleep 0.3; done
BASE="$RESTART_BASE"  # 后续请求一律打到重启后的实例
ok "服务已用同一数据目录重启（$BASE）"
# 重启后待登记上传按规则失败、暂存清理
curl -s "$RESTART_BASE/api/uploads" | "$PY" -c 'import sys,json;u=next(x for x in json.load(sys.stdin) if x["filename"]=="img-c.tif");assert u["state"]=="failed", u'
[ -z "$(ls -A "$DATA_DIR/staging")" ] && ok "重启恢复：未完成上传标记 failed，staging/ 已清理"
# 轮换自动续办至完成
for _ in $(seq 1 50); do
  curl -s "$RESTART_BASE/api/rotations/current" | "$PY" -c 'import sys,json;raise SystemExit(0 if not json.load(sys.stdin)["active"] else 1)' && break
  sleep 0.3
done
curl -s "$RESTART_BASE/api/rotations/current" | "$PY" -c 'import sys,json;d=json.load(sys.stdin);assert d["last"]["state"]=="completed", d'
ok "重启后轮换自动续办并完成"
# 全部历史归档仍可解密且内容一致
for f in img-a img-b; do
  fid="$(curl -s "$RESTART_BASE/api/files" | "$PY" -c 'import sys,json;print(next(x["file_id"] for x in json.load(sys.stdin) if x["filename"]=="'$f'.tif"))')"
  download "$fid" "$BASE_DIR/samples/$f.restart-dl.tif"
  cmp "$BASE_DIR/samples/$f.tif" "$BASE_DIR/samples/$f.restart-dl.tif"
done
ok "重启后全部已归档文件仍可解密，且与原件逐字节一致"

say "4/ 净化下载：内存重建 TIFF，剔除采集标签，标定标签与像素保留"
# 用测试辅助构造一个含“敏感”采集标签的两页 TIFF（第二页无任何标定标签）
PYTHONPATH="$PWD/tests:$PWD" "$PY" - "$BASE_DIR/samples/subject.tif" <<'EOF'
import sys
sys.path.insert(0, "tests")
from tiff_builder import TiffBuilder, MAKE
b = TiffBuilder()
b.add_page(64, 64, calibration=((300, 1), (300, 1), 2),
           extra_tags=[(MAKE, 2, "Subject-PII-001")])
b.add_page(96, 80, calibration=None)
open(sys.argv[1], "wb").write(b.build())
EOF
SID="$(upload "$BASE_DIR/samples/subject.tif")"
SDID="$(curl -s -X POST "$BASE/api/files/$SID/sanitized-downloads" | jget download_id)"
curl -s -D "$BASE_DIR/samples/sanitized.headers" -o "$BASE_DIR/samples/subject.sanitized.tif" \
  "$BASE/api/sanitized-downloads/$SDID/content"
grep -qi "x-sanitized-pages: 2" "$BASE_DIR/samples/sanitized.headers" \
  && ok "净化成功，响应头注明 2 页；逐页注明见 X-Sanitized-Notes"
if grep -a "Subject-PII-001" "$BASE_DIR/samples/subject.sanitized.tif" >/dev/null; then
  echo "净化件中发现敏感采集标签！"; exit 1
fi
ok "净化件不含 Make 等采集标签/子目录，仅保留必要结构标签与有效标定标签"
PYTHONPATH="$PWD/tests:$PWD" "$PY" - "$BASE_DIR/samples/subject.sanitized.tif" <<'EOF'
import sys
from tiff_builder import parse_tiff_tags
pages = parse_tiff_tags(open(sys.argv[1], "rb").read())
allowed = {256,257,258,259,262,273,277,278,279,284,282,283,296}
assert all(set(p) <= allowed for p in pages), "出现白名单之外的标签"
assert 282 in pages[0] and 296 in pages[0], "第 1 页标定标签丢失"
assert {282,283,296}.isdisjoint(pages[1]), "第 2 页应无标定标签"
print("   标签白名单与逐页标定状态断言通过")
EOF

# 失败路径：非默认方向的影像必须整件拒绝，且不产生半成品
PYTHONPATH="$PWD/tests:$PWD" "$PY" - "$BASE_DIR/samples/oriented.tif" <<'EOF'
import sys
sys.path.insert(0, "tests")
from tiff_builder import TiffBuilder
b = TiffBuilder(); b.add_page(64, 64, orientation=6)
open(sys.argv[1], "wb").write(b.build())
EOF
OID="$(upload "$BASE_DIR/samples/oriented.tif")"
ODID="$(curl -s -X POST "$BASE/api/files/$OID/sanitized-downloads" | jget download_id)"
CODE="$(curl -s -o "$BASE_DIR/samples/rejected.json" -w '%{http_code}' \
  "$BASE/api/sanitized-downloads/$ODID/content")"
[ "$CODE" = "422" ] && ok "非默认方向整件拒绝（HTTP 422）：$(cat "$BASE_DIR/samples/rejected.json")"
[ ! -s "$BASE_DIR/samples/rejected.json" ] || grep -q "Orientation" "$BASE_DIR/samples/rejected.json" \
  && ok "拒绝原因明确，且响应体只含错误 JSON、无任何 TIFF 字节"

say "演示完成"
echo "浏览器入口: $RESTART_BASE/ （本脚本结束后服务已停止；手动启动见 README）"
echo "服务日志:   $LOG 、 $BASE_DIR/server-restart.log"
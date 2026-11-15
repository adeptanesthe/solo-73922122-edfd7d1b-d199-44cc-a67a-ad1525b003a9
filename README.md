# 科研影像加密归档（单课题 · 单进程）

FastAPI + SQLite + `cryptography` 实现的单课题 TIFF 加密归档网页：每件影像以独立随机
数据密钥（DEK）做 AES-256-GCM 加密，DEK 再由课题密钥（KEK，AES-256-GCM）封装后入库；
密文与课题密钥**分目录持久保存**，原件明文与数据密钥**绝不落盘**。支持上传、原件下载、
**净化下载（内存解密并重建 TIFF，剔除采集描述等受试者信息）**、课题密钥轮换及进度查看，
轮换期间上传下载照常，重启自动续办。

## 一、快速开始

### 方式 A：Docker Compose（推荐）

```bash
docker compose up --build
# 浏览器打开
open http://localhost:8000/        # macOS
# 或直接访问 http://localhost:8000/
```

数据全部持久化在命名卷 `archive-data` 内（容器内 `/data`）：

```
/data
├── keys/      # 课题密钥文件 *.kek（权限 600），与密文分目录
├── vault/     # 已归档密文 *.blob（nonce || GCM 密文+tag）
├── staging/   # 上传暂存密文（登记成功后原子移入 vault/，重启清理孤儿）
└── archive.db # SQLite（WAL）：文件/上传/下载/轮换元数据与“封装后的 DEK”
```

停止与清理：

```bash
docker compose down            # 停止，保留数据卷
docker compose down -v         # 同时删除数据卷（清空全部归档）
```

### 方式 B：本地 Python 运行（无需 Docker）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
ARCHIVE_DATA_DIR=./data uvicorn app.main:app --host 127.0.0.1 --port 8000
# 浏览器入口：http://127.0.0.1:8000/
```

## 二、浏览器入口能做什么

打开 `http://localhost:8000/`（单页应用，原生 JS，无构建步骤）：

1. **课题密钥与轮换**：查看当前 KEK、`keys/` 目录状态，一键发起轮换，实时进度
   （重封装件数、阻塞中的上传/下载数、旧钥文件是否已删除）；
2. **演示控制（暂停点）**：可分别暂停/恢复“上传待登记”和“下载待解封”两个闸门；
3. **上传**：选择 `.tif/.tiff`（每件 ≤ 8 MiB），页面展示三阶段状态机；
4. **已归档影像与下载**：查看每件影像当前封装在哪个 KEK 下，可发起**原件下载**或
   **净化下载**，并查看两类下载会话的状态、逐页注明与失败原因；
5. **净化下载说明与操作日志**：净化规则与边界见下节，所有关键动作带时间戳输出。

## 三、净化下载（交给合作组，不带走受试者信息）

在影像行点击「净化下载」，服务端在**内存中**解封原件并重建一份全新 TIFF，原归档密文与
封装关系不变，明文与 DEK 全程不落盘（与原件下载共用「下载待解封」暂停点和受理快照机制）。

### 保留与剔除

- **逐页保留**：解码后的 8 位灰度像素（逐字节）、页序，以及完整有效的
  `XResolution`、`YResolution`、`ResolutionUnit` 三枚标定标签（含 RATIONAL 分数值）。
- **输出只含**必要结构标签（ImageWidth/ImageLength/BitsPerSample/Compression/
  PhotometricInterpretation/StripOffsets/SamplesPerPixel/RowsPerStrip/
  StripByteCounts/PlanarConfiguration）与上述标定标签；统一重排为小端、每页单条带的
  未压缩 TIFF。
- **不带出**任何其他源标签（如 Make/Software 等采集描述）或子目录（SubIFD）。

### 适用范围与整件拒绝规则

- 支持 1～3 页、每页宽高 64～512 像素的 8 位灰度（单样本、未压缩）Classic TIFF（II/MM 均可）。
- **某页损坏（IFD/条带越界、字节数不符等）、方向非默认（Orientation 存在且 ≠1）、
  或标定标签残缺/无效时，整件拒绝**：下载会话标记 `failed` 并记录中文原因，HTTP 返回
  422，**不产生任何可下载的半成品**；失败会话同样清空受理快照中的封装材料。
- 标定标签三枚必须同时存在且有效（RATIONAL 分子分母为正、ResolutionUnit ∈ {1,2,3}）；
  **三枚完全缺失的页可正常处理**，并在会话记录与响应头 `X-Sanitized-Notes` 中注明
  「本页无任何标定标签」。
- 输出文件名固定为 `<原文件名去扩展>.sanitized.tif`，响应头另带 `X-Sanitized-Pages`
  与 `X-Sanitized-Notes`（逐页注明，URL 编码的 JSON）。

### 与密钥轮换的关系

净化下载与原件下载一样在受理时快照 KEK 标识与封装 DEK：已受理的净化读取遇到课题密钥
轮换，仍能用旧快照读完该件原件并完成重建；轮换收尾会等待该在途会话结束后才删除旧 KEK。

## 四、本地演示脚本

脚本自动启动服务并完整演示题目要求的三类场景：

```bash
pip install -r requirements.txt
PORT=8090 ./scripts/demo.sh /tmp/demo
```

演示内容：

| 场景 | 演示断言 |
| --- | --- |
| 正常上传/下载 | 逐字节一致；vault/staging 无明文 TIFF 魔数；密钥与密文分目录 |
| **上传待登记暂停** | 闸门关闭后上传停在 `pending_register`（DEK 仅在内存）；轮换停在收尾且 `blocking_uploads=1`；恢复后该上传由**新 KEK** 封装，不留下旧钥引用 |
| **下载待解封暂停** | 已受理下载停在 `accepted`；轮换 `blocking_downloads=1` 且旧 KEK 文件保留；恢复后在途下载读完原件，轮换才完成 |
| **轮换中重启** | 轮换未完成时退出；重启后自动续办至完成、未完成上传按规则失败并清理暂存、全部已归档文件仍可解密且与原件一致 |

受限沙箱（禁止 `kill` 系统调用）中用“同一数据目录 + 新端口新进程”模拟重启：

```bash
PORT=8092 PORT2=8093 RESTART=report ./scripts/demo.sh /tmp/demo
```

恢复逻辑与进程重启完全相同（都是重新执行 `Service._recover()`），仅进程替换方式不同。

## 五、测试

```bash
pip install -r requirements-dev.txt
pytest -q
```

共 24 个端到端测试（FastAPI TestClient + 真实 SQLite/文件系统）。原 9 个归档/轮换测试覆盖：
明文不落盘不变量、8 MiB 上限、轮换只重封装 DEK 且密文逐字节不变、旧 KEK 文件删除后才报完成、
两个暂停点、在途下载读完原件、同期只允许一次轮换、轮换中重启续办、重启清理暂存。
新增 15 个净化测试（`tests/test_sanitize.py`，配合手工 TIFF 构造器 `tests/tiff_builder.py`）
覆盖：逐页像素/页序/标定保留、敏感标签与 SubIFD 不带出、II/MM 字节序、无标定页注明、
标定残缺/无效拒绝、非默认方向拒绝、损坏页/截断/魔数错误拒绝且无半成品、1～3 页与 64～512
尺寸边界、非灰度/压缩/位深拒绝、明文与 DEK 不落盘且原归档不变、在途净化读取穿越密钥轮换、
失败不影响后续下载、重启后受理会话中断且可重新发起；另含旧版 downloads 表的 schema 迁移验证。

## 六、HTTP API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/uploads` | 受理上传（filename、size，>8 MiB 返回 413） |
| PUT | `/api/uploads/{id}/content` | 上传密文前置：服务端用内存随机 DEK 加密并暂存 |
| POST | `/api/uploads/{id}/complete` | **待登记暂停点** → 用当前 KEK 封装 DEK 并登记归档 |
| POST | `/api/uploads/{id}/abort` | 中止上传并清理暂存 |
| POST | `/api/files/{file_id}/downloads` | 受理原件下载，快照 KEK 标识与封装 DEK |
| GET | `/api/downloads/{id}/content` | **待解封暂停点** → 解封并读完原件，返回明文 |
| POST | `/api/downloads/{id}/abort` | 中止下载会话 |
| POST | `/api/files/{file_id}/sanitized-downloads` | 受理净化下载，同样快照 KEK/封装 DEK，预定输出名 |
| GET | `/api/sanitized-downloads/{id}/content` | 内存解封→解析校验→重建净化 TIFF；失败返回 422 且无字节 |
| POST | `/api/rotations` | 发起轮换（已有轮换时 409） |
| GET | `/api/rotations/current` | 轮换状态与进度（页面 1.5s 轮询） |
| GET | `/api/files` `/api/keys` `/api/uploads` `/api/downloads` | 列表查询（downloads 含 kind/output_name/error/page_count/notes） |
| POST | `/api/demo/gates/{upload_register,download_unwrap}` | 演示暂停门 `{"paused": true/false}` |

## 七、设计要点与安全不变量

1. **两级密钥**：每件影像独立随机 DEK（32 B，仅存在于进程内存）；KEK（32 B）保存在
   `keys/*.kek`（带魔数头、权限 600、原子写 + fsync）。封装与原件加密使用不同的 GCM
   nonce 与 AAD，防止消息类型混用。
2. **明文不落盘**：上传内容读取后立即在内存加密，临时文件与最终 blob 均为
   `nonce || 密文||tag`；下载明文只随 HTTP 响应返回，不写磁盘。
3. **轮换只动封装**：`files.blob_path/nonce`（原件密文）永不修改，仅
   `unwrap(旧KEK) → wrap(新KEK)` 更新 `wrapped_dek/dek_nonce/current_kek_id`，每件独立
   事务提交，天然可中断、可续办。
4. **同期只轮换一次**：存在 `running/completing` 轮换时再次发起直接 409；旧 KEK 发起
   瞬间置 `rotating`，新上传恒封装到新 active KEK，**收尾后不存在旧钥引用**。
5. **完成条件（全部满足才报完成）**：无文件引用旧 KEK、无待登记/在途上传、无在途下载、
   数据库无残留的旧 KEK 封装材料，最后**实际删除旧 KEK 文件**才写 `completed`。
6. **在途下载可读**：受理下载即快照 KEK 标识与封装 DEK，与 `files` 行随后的重封装解耦；
   下载完成后立即清空快照封装材料。
7. **暂停门与死锁规避**：闸门关闭时会话在检查点等待，等待期间释放全局锁并在唤醒后复查
   状态，避免与闸门控制请求死锁；服务关闭可在 1 个轮询周期内打断等待。
8. **崩溃恢复（`_recover`）**：未完成上传（内存 DEK 已随进程消失）标记 `failed` 并清理
   暂存；未完成下载标记 `interrupted`；清理孤儿暂存；未完成轮换重置为 `running` 并由
   后台线程续办；已归档文件的 DEK 始终能被某个未删除的 KEK 解封，因此**始终可解密**。
9. **并发模型**：单进程多线程，所有 SQLite 访问与共享状态在一把可重入锁内串行化，
   SQLite 另开 WAL + `busy_timeout` 兜底；密钥文件、blob 均为原子替换写入。
10. **净化重建（`app/tiff.py`，纯标准库）**：先完整解析并逐页校验全部页面，再一次性重建
    输出，任一页失败即抛错且不返回部分结果；解析与重建只在内存进行，不引入第三方图像库。
    净化会话复用下载状态机（accepted → unwrapping → completed/failed），失败也是终态并
    清空封装材料，因此不残留旧 KEK 引用、不阻塞轮换收尾。

## 八、目录结构

```
app/
├── crypto.py          # AES-256-GCM 原语：DEK/KEK 生成、封装/解封、密钥文件原子读写
├── tiff.py            # TIFF 解析/校验与净化重建（纯标准库，明文仅在内存）
├── db.py              # SQLite schema（含 downloads 表在线迁移）
├── service.py         # 状态机、轮换工作线程、暂停门、崩溃恢复（核心）
├── main.py            # FastAPI 路由与 lifespan
└── static/index.html  # 浏览器单页应用
scripts/demo.sh        # 三类场景的本地演示脚本
tests/test_archive.py  # 9 个归档/轮换端到端测试
tests/test_sanitize.py # 15 个净化下载端到端测试
tests/tiff_builder.py  # 手工 Classic TIFF 构造/读回辅助
Dockerfile, docker-compose.yml
```

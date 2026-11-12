# 需求文档：转发与传输的统一 ID 分配 + 恢复通道合并

状态：**已被 [统一实施计划](transfer-fid-and-resume-unify-plan.md) 替代，仅保留为历史草案，不是实施依据。** optional-target、kind 及 Client 专用恢复表等旧方案不得沿用；当前授权范围见实施计划更改 4。

---

## 1. 背景与问题

原实现中，文件传输、文件夹传输、中继转发三条链路各自维护传输 ID（fid），存在以下问题：

- **D1 收/发同号**：`file_client_id` 与 `_forward_fid` 都从 0 起，入方向 ID 来自对端、出方向 ID 来自本端，两者可能数值相同。`_active_transfer_fids`、`_active_transfer_sockets`、`_transfer_pause` 等共享注册表以 fid 为键，导致一方重连的清理误关另一方健康连接。实测：`_drop_stale_transfers(peer=X)` 误关发往 Y 的健康连接。
- **D2 重启复用**：计数器每次启动从 0 起，重启后的新腿可能复用旧进度表中仍待恢复的 ID，覆盖其条目。
- **D3 中继与普通共空间**：`_resume_request` 靠“先查中继表，查不到再走普通恢复”的顺序消歧，误判会路由错误。
- **计数器递减**：腿结束时 `-= 1` 归还 ID，与分配点不配对，转发上传腿结束时会把别人的号减掉。
- **转发恢复不幂等**：`_resume_forward` 在重放前删除记录，重放中二次断线后无法再次恢复。
- **`/forward_resume` 与 `/resume_trans` 功能重叠**：两条命令承载同样的信息，增加代码路径。

## 2. 目标

1. 用单一分配器 `_FidAllocator` 替换两套计数器，ID 在实例间（随机 epoch）与重启间（新 epoch）不冲突，且同实例内单调不重号。
2. 删除腿结束时的归还逻辑。
3. 删除 tfid：转发时 server 不为目标预留 ID，目标 client 用自己的分配器分配 fid 并回报。
4. 删除 kind 位：ID 低位固定，不参与任何判断。
5. 废弃 `/forward_resume`，能力并入 `/resume_trans` 的可选目标字段。
6. 转发识别改为 server 在 `/resume_trans` 时校验原始对端：不是自己即为转发消息。
7. 转发重放沿用原 fid，使“成功才删”替代“先删后放”，达到幂等。

## 3. 非目标

- 不改 `/file`、`/file_folder` 既有字段语义（只允许新增可选字段）。
- 不改进度表 JSON 结构、不改字节传输协议与加密帧格式。
- 不做中继落盘（server 仍不触碰磁盘）。
- 不做暂停状态跨重连持久化。
- 不解决“上传/转发恢复依赖固定 `client_port`”（另开任务，仅补文档）。
- 不做注册表、种子、僵尸扫描、区间压缩（已论证不需要）。

## 4. 已确认的决策

| # | 决策 | 依据 |
|---|---|---|
| 1 | epoch 为随机 31 位，每实例一次 | 时间戳同毫秒对撞 1977/2000；随机 2⁻³² 级碰撞 |
| 2 | counter 每进程从 0 起，无硬上界，置于高位 | counter 置低位时超过 2³³ 会溢出污染 epoch；高位无此问题 |
| 3 | 不做种子、不做注册表、不做僵尸扫描 | 注册表 20 万 ID 常驻 18.3 MiB，计数器恒定 0.3 KiB；种子多一条失败路径 |
| 4 | 唯一性由三表检查 + 随机 epoch 保证 | 三表检查确定性拦截同进程冲突 |
| 5 | 不做内存压缩 | 在飞 ID 规模由并发数决定（几十条） |
| 6 | 原 bit 31 的 dir/kind 位取消 | 实测 dir 位对入/出冲突无保护作用；kind 位判断后又被取消 |
| 7 | `/forward_resume` 废弃，并入 `/resume_trans` | 两者承载信息相同 |
| 8 | tfid 取消，目标 client 分配自身 fid 并回报 | 每条腿的 ID 只由分配该腿的一端产生，链路只剩一套来源 |
| 9 | 分配归属不变量：client→server 的 fid 由 server 分配；server→目标的 fid 由目标 client 分配 | 同上 |
| 10 | kind 位取消，低位固定为 0 | 转发识别不依赖 ID 位 |
| 11 | 转发识别：`/resume_trans` 到达 server 时若 `record["originator"] != 请求方` 则为转发消息 | 单次字段比较，无需位判断 |
| 12 | server 发 `/file` 时不带占位 ID，由目标在回报中给出 fid | 避免占位值被误用 |
| 13 | 重放时 server 向源端转发的 `/resume_trans` 保留目标原始地址 | 源端需要校验请求目标 |

## 5. 设计

### 5.1 ID 布局

```
 63                          32 31                1 0
┌──────────────────────────────┬───────────────────┬───┐
│      counter（无上界）        │   epoch (31b)     │ 0 │
└──────────────────────────────┴───────────────────┴───┘
fid = (counter << 32) | (epoch << 1)
```

- 最低位恒为 0，保留为扩展位，不参与任何判断。
- 线路上仍是十进制整数；`int()`、`shlex.split`、JSON 键 `str(fid)` 均无位宽要求（实测 20–61 位可往返）。
- 日志格式 `format_fid(fid) = e<epoch>.<counter>`。

### 5.2 分配器

```python
class _FidAllocator:
    def __init__(self, live_tables):
        self._epoch = int.from_bytes(os.urandom(4), "big") & 0x7FFFFFFF
        self._counter = 0
        self._live_tables = live_tables      # 引用，不复制
        self._lock = threading.Lock()

    def alloc(self):
        with self._lock:
            while True:
                self._counter += 1
                fid = (self._counter << 32) | (self._epoch << 1)
                if not any(fid in t for t in self._live_tables):
                    return fid
```

`live_tables` 为 `(file_upload_progress, file_recv_progress, _forward_resume_records)`。

**锁约束**：调用 `alloc()` 时不得持有 `_transfer_progress_lock` 或 `_forward_relays_lock`。

### 5.3 分配归属

| 腿 | 分配者 | 回报 |
|---|---|---|
| 发送 client → server（上传/普通） | server（入方向，对端 client 发来的 fid 由 server 登记） | 原有命令沿用 |
| server → 目标 client（投递） | 目标 client | `/server_file_transfer_port <port> <fid> <stamp>` |
| 公钥推送 | 推送方 | 沿用 |

说明：文件上传时 client 目前仍由自身分配器产生 fid 并发给 server（提交 1 的现状）。按决策 9，**client→server 的 fid 应由 server 分配**。这一点与提交 1 的现状不一致，需在提交 3 中一并调整，并在本文件 §8 标注。

### 5.4 `/resume_trans` 路由

```
server 收到 /resume_trans <fid> <offset>（来自请求方 R）：
  record = _forward_resume_records.get(fid)
  if record is not None and record["originator"] != R:
      # 转发消息：R 是目标，原始对端是 originator
      转发给 originator：/resume_trans <fid_源> <offset> <R 的地址>
  else:
      resume_trans(R, fid, offset)   # 直连恢复
```

- 判据只有一次字段比较，不依赖 ID 位。
- 源端收到 4-token 形式的 `/resume_trans <fid> <offset> <target>` 时，调用 `_resume_forward(target, fid, offset)`。

### 5.5 转发握手（新顺序）

1. A → S：`/forward_item file report.pdf (C)`。
2. S 分配源端 fid_S（上传腿，server 分配）。
3. S 向 C 发 `/file report.pdf (A)`（不带 ID，决策 12）。
4. C 分配 fid_C，回 `/server_file_transfer_port <port_C> <fid_C> <stamp>`。
5. S 收到后登记 `_forward_resume_records[fid_S] = {originator: A, targets: [C], fid_targets: {C: fid_C}}`，并以 fid_C 作为该目标的投递键。
6. S 向 A 发 `/forward_upload <fid_S> <sport> <stamp>`；A 开始上传；S 扇出给 C（键 fid_C）。
7. 完成后 S 删除 record；A 删除 upload 进度。

等待点由“等预先登记的端口”改为“等目标回报 fid+端口”，额外代价为一次往返。

### 5.6 重放（幂等）

```
C 残文件 → offset
C --/resume_trans <fid_C> <offset>--> S
S: record 存在且 originator≠C → 转发
S --/resume_trans <fid_S> <offset> (C)--> A
A: _resume_forward(C, fid_S, offset)   # 只读记录，不删
   resuming 已含 C → 只记日志
   否则 _forward_one(..., fid=fid_S, resume_offset=offset)
     --/forward_item file resume=<offset> ...--> S
S: 沿用 fid_S；向 C 投递，键仍为 fid_C（C 侧沿用原 fid）
成功：A 从记录移除 C；S 从 record 移除 C；空则删
```

- `relay_fid`/`tfid` 参数取消：fid_S 由 S 侧记录沿用，fid_C 由 C 侧沿用。
- C 的 `file_recv_progress` 键在恢复前后不变，幂等由“键稳定 + 成功才删”保证。

## 6. 变更文件列表

| 文件 | 动作 | 内容 |
|---|---|---|
| `PyFlow/network_api/connect_tcp.py` | 修改 | 分配器去 kind；删除 `FID_KIND_*`、`fid_kind`；`_resume_request` 改为 originator 校验；删除 `FORWARD_RESUME_LINE`、`parse_forward_resume_command`、`handle_server_command` 对应分支；`_forward_relay` 改为目标回报 fid；`_resume_forward` 去掉先删后放；`_forward_one` 接受既有 fid；模块 docstring 第 20–30 行同步 |
| `test/integration/network_api/test_tcp_transfer_resume.py` | 修改 | 删除硬编码奇偶 fid 约定；新增 D1/D2、C5 二次断线、C6 多目标用例；握手顺序测试 |
| `test/integration/network_api/test_tcp_file_transfer.py` | 修改 | 确认 fid 取值变化后的断言不依赖具体值 |
| `test/integration/test_forward_extension.py` | 修改 | 转发握手改为目标回报 fid |
| `test/unit/network_api/test_fid_allocator.py` | 新增 | 不变量：唯一、跨实例不同、跨重启不同、三表跳过、`format_fid` 可读 |
| `test/unit/network_api/test_resume_matching.py` | 修改 | `/resume_trans` 3/4 token 解析；originator 校验分支 |
| `docs/source/Network_APIs/TCP_Server_APIs.rst`、`TCP_Client_APIs.rst` | 修改 | `/resume_trans` 可选字段；fid 归属；`client_port` 与恢复关系 |
| `docs/source/File_Transfer/File_Transfer.rst` | 修改 | 转发握手与恢复链路；移除 `/forward_resume` |
| `docs/source/changes/transfer-fid-allocator.md` | 新增 | 变更说明：协议不兼容点、fid 取值域变化 |
| `docs/plans/transfer-fid-and-resume-unify-plan.md` | 删除 | 由本需求文档取代 |

## 7. 公共契约变更

| 契约 | 前 | 后 | 兼容性 |
|---|---|---|---|
| `fid` 取值 | 小整数 | `(counter<<32)\|(epoch<<1)`，20–61 位十进制 | **不兼容**：外部假设小整数会失效 |
| `fid` 分配者 | 发送方各自分配 | 上传由 server 分配；投递由目标分配 | **不兼容** |
| `/server_file_transfer_port` | `<port> <fid> <stamp>`，fid 为 server 预留 | 同格式，fid 为目标自分配值 | 格式兼容，语义变化 |
| `/resume_trans` | 3 token | 3 或 4 token（4 token 带目标地址） | 读兼容；旧实现收 4 token 丢行 |
| `/forward_resume` | 独立命令 | 删除 | **不兼容** |
| `/forward_item` | 无 ID 参数 | 无新增参数（重放沿用记录，不需 relay_fid/tfid） | 兼容 |
| `_forward_alloc_fid` | 公开方法 | 已删除（提交 1） | 外部调用方需改 |

## 8. 已知偏差（需在提交 3 处理）

提交 1 中，上传腿的 fid 仍由 client 的分配器产生并发给 server。按决策 9，上传腿的 fid 应由 server 分配。提交 3 需要：
- server 接收上传时为该腿分配 fid，回报给 client；或
- 将上传握手改为 server 先给出 fid。

这是提交 1 的遗留问题，提交 3 合并时必须修正，否则归属不变量不成立。

## 9. 原子提交拆分

| # | 意图 | 主要路径 | 预估 | 局部验证 |
|---|---|---|---|---|
| 1（已完成） | 分配器 + 删两处归还 + 替换四处分配 | A1–A4, B1–B2 | ~130 行 | 已通过：10 + 53 + 22 条 |
| 2 | 删除 kind 位；ID 布局改为 `(counter<<32)\|(epoch<<1)`；`_resume_request` 改为 originator 校验 | D3, C3 路由 | ~60 行 | resume 套件全绿；originator 校验两分支单测 |
| 3 | 转发握手改为目标分配 fid（删 tfid 预留）；上传 fid 改由 server 分配（修正 §8） | A5, D1, §8 | ~120 行 | 转发集成测试全绿；握手顺序测试；上传归属测试 |
| 4 | `/forward_resume` 并入 `/resume_trans`（4-token 目标字段） | C3, C4 | ~70 行（净负） | 抓取 4-token 行；resume 套件全绿 |
| 5 | 重放沿用原 fid；删除 `_resume_forward` 先删后放 | A6, B3, B4, C5 | ~50 行 | 二次断线测试 |
| 6 | 多目标粒度测试；文档与变更说明；删除旧计划文件 | C6 | ~130 行 | 一成功一失败；文档构建 |

提交 2 必须先合；提交 3 依赖提交 2；提交 4/5 依赖提交 3；提交 6 最后。

每个提交附报告：意图、改动内容、为什么、局部验证、明确未覆盖、风险。

## 10. 验收证据

1. D1 回归：入方向 fid 与出方向分配器 ID 不同；`_drop_stale_transfers(peer=X)` 不关闭发往 Y 的腿。
2. D2 回归：新实例首批 ID 与读回的旧 ID 不同。
3. 转发握手：目标回报 fid 前 server 不向源端开放上传；回报后才发 `/forward_upload`。
4. C3/C5/C6 真实 socket 集成测试。
5. 基线 64 条相关测试全绿（提交 1 后为 10 + 53 + 22 条，已验证）。
6. 日志中一次转发恢复出现不带位判断的 4-token `/resume_trans`。

## 11. 风险

| 风险 | 影响 | 应对 |
|---|---|---|
| 协议握手顺序变化（新增目标回报往返） | 旧版本互不兼容 | `changes/` 标注；不提供兼容期 |
| ID 不含来源位，来源判断只能靠表或 originator 字段 | 判断逻辑必须覆盖两分支 | 单测分别覆盖 originator 为自己与非自己 |
| 目标分配 fid 后、回报前断线 | 该 fid 未登记，属于未开始的投递 | 单测确认无残留 |
| 上传归属修正（§8）影响提交 1 的测试 | 测试中预测 ID 的写法失效 | 提交 3 改为从回报中读取 fid |
| 三表检查锁顺序 | 死锁 | `alloc` 不得在两个 progress 锁内调用，逐点核查 |

### 未覆盖（本次不做）

- 中继落盘；暂停状态持久化；`is_enable_file_transfer_progress_bar=False` 时无法恢复；恢复对固定 `client_port` 的依赖（仅补文档）。

## 12. 对 CI / 测试 / 依赖 / 公共接口的影响

- CI：否。
- 测试：新增 3 类，不删不改既有断言（测试中硬编码奇偶 fid 的写法会被删除，属于约定变更，需在提交 2 标注）。
- 依赖：无新增。
- 公共接口：**是**。

## 13. 待人类确认

确认本文件后，从提交 2 开始实施，每个提交单独报告并等待放行。
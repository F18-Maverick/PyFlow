# 统一端口分配与 socket 创建接口计划

状态：需求方向已确认：新增内部统一 socket 分配接口，保留现有 `palloc()` / `pfree()` 的整数与释放契约。当前仅完成调查，未为本计划修改产品代码；工作区已有前一轮 Windows 10013 修复及其测试/文档改动，本计划将重构该实现，避免再叠加旁路。

## 1. 需求卡

### 目标

在不破坏公共 `palloc()` / `pfree()` API 的前提下，统一“逻辑端口候选 → 真实 socket bind/connect → 实际端口 → 释放逻辑预留”的内部路径。

新增内部 socket 分配层，供以下路径复用：

- 文件传输发送端：server/client `file_transfer_mode()` 的 bind/connect。
- 文件传输接收端：server/client `file_transfer_mode_recv()` 的 listener bind 与实际端口公告。
- forward relay 的 upstream listener bind 与实际端口公告。
- 两类 `create_temporary_server()` listener，以及客户端 `create_temporary_client()` 的 palloc 绑定路径。
- 当前已有的 Windows `EADDRINUSE`/10048/10013 回退统一收口，不保留 `_bind_local_port_with_fallback()` 与发送端重复的两套规则。

主控制连接的固定 `client_port` 不经过 `palloc()`：客户端必须先建立控制连接，服务端的 `/client_alloc_port_range` 协商之后才有客户端分配范围。因此控制连接继续使用同一内部 bind/connect 规则，但保留其专用重连循环，不伪造 palloc 预留。

### 非目标

- 不把 `palloc()` 改成返回 socket、tuple 或 lease；不改变 `palloc()` 自动模式返回 `0`、手动模式返回整数端口的现有契约。
- 不让 `pfree()` 负责关闭 socket；它继续只释放逻辑分配记录。socket 生命周期仍由调用路径和 transfer registry 管理。
- 不在 `palloc()` 内提前 bind 探测：探测与真正使用之间存在 TOCTOU，不能解决 Windows 保留端口，也会改变公共分配 API 的语义。
- 不改变服务端主 listener、无固定本地端口的 relay target connect、加密、FID、协议 marker、文件内容、重试次数或 CI/依赖。
- 不以临时兼容别名保留第二套分配入口；迁移完成后删除重复的端口回退 helper/逻辑。

### 受影响范围

- `PyFlow/network_api/connect_tcp.py`：内部 socket/port lease helper；server/client 的 temporary、file transfer、relay、控制连接调用点；现有 `palloc()`/`pfree()` 实现保留。
- `test/unit/network_api/test_TCP_server.py`、`test/integration/network_api/test_tcp_broadcast_alloc.py`：保留并扩展 palloc/pfree 整数语义、手动范围、自动模式和释放行为契约。
- `test/integration/network_api/test_tcp_file_transfer.py`、`test/integration/network_api/test_tcp_transfer_resume.py`：真实 loopback socket 的 bind/connect 回退、listener 实际端口公告、relay 清理。
- 可能新增 `test/unit/network_api/test_socket_port_allocation.py`：只测试内部 helper 的端口 lease 状态机，不替代真实 TCP 集成证据。
- `docs/source/Port_Allocation/Port_Allocation.rst`、`docs/source/Network_APIs/TCP_Client_APIs.rst`、`docs/source/Network_APIs/TCP_Server_APIs.rst`：区分“逻辑端口分配 API”和“真实 socket 分配层”，记录实际端口与回退语义。
- `docs/plans/unified-port-socket-allocation-report.md`：实施与验收报告；更新前一轮 10013 计划中的实现状态/链接，避免两份报告描述不同实现。

### 安全与数据边界

socket 分配层只处理本地地址、端口、socket 生命周期和本地日志；不读取、记录或改变 payload、密钥、文件内容、FID 或远端鉴权数据。所有 socket 仍必须经过现有 `_transfer_handshakes`、加密 attach、统一 send/receive 入口。

风险等级：中。风险集中在 socket 替换时的 registry 释放、palloc 记录释放、listener 端口公告和并发重试。

## 2. 内部接口与数据流

### 2.1 保留的公共分配契约

`palloc()` / `pfree()` 保持不变：

- 自动模式：`palloc() -> 0`，由 socket bind(`0`) 让内核选择实际端口；`pfree(0)` 无操作。
- 手动模式：`palloc() -> int`，只记录候选端口；`pfree(int)` 删除逻辑记录并调整现有游标。
- palloc 不保证端口已被 OS bind；真实可用性由下一层 socket bind/connect 确认。

### 2.2 新的内部 socket 分配层

增加一个模块级私有入口（名称以实现时为准，计划契约称为 `_open_port_socket`）：

```text
_open_port_socket(
    bind_host,
    requested_port,
    *,
    connect_to=None,
    configure=None,
    register=None,
    release=None,
) -> (socket, actual_port, used_fallback)
```

约束：

1. `requested_port` 是调用方已经从 `palloc()` 得到的候选整数，或显式固定端口；该层不改变 palloc 返回类型。
2. helper 创建新 socket并重复应用 `SO_REUSEADDR`、timeout 等配置；若提供 `connect_to`，在 bind 后 connect。
3. bind 或 connect 报本地端口冲突（`EADDRINUSE`、WinError 10048/10013、Windows-only `errno.EACCES`）且候选端口非零时：
   - 释放/关闭失败 socket及其 registry 记录；
   - 创建全新 socket；
   - 用 bind port `0` 重试；
   - 返回 `used_fallback=True` 与 `getsockname()` 的实际端口。
4. 非端口冲突错误原样交给现有 timeout/refused/重试分支；未绑定/端口 `0` 不触发“固定端口回退”。
5. `register`/`release` 回调只用于 transfer registry 生命周期；helper 不绕过 `_TransferHandshakeRegistry`。
6. 调用方负责在完整 socket 生命周期结束后调用现有 `pfree(requested_port)`；helper 不让 `pfree` 关闭 socket，也不留下“OS 实际端口”到逻辑分配表。

### 2.3 数据流

```text
palloc() -> requested candidate
       -> _open_port_socket: configure -> bind(candidate) -> connect(optional)
          success -> actual_port -> listener announcement / transfer handshake
          local conflict -> close + registry release -> bind/connect from port 0
                       -> actual_port -> same announcement / handshake
       -> socket close -> existing pfree(requested candidate)
```

控制连接：`client_port` 是显式固定值或 `None/0`，不产生 palloc lease；仍由 `TCP_Client_Base.connect()` 的 wait/reconnect 循环调用同一 socket 规则。

## 3. 全部执行路径与修改点

| 路径 | 当前来源 | 统一入口后的行为 | 必须验证 |
|---|---|---|---|
| server/client `palloc` 自动模式 | `palloc() == 0` | bind(0)，返回真实 ephemeral port；pfree(0) 不变 | 实际 `getsockname()[1] > 0` |
| server/client 手动模式候选可用 | `palloc() -> concrete` | bind/connect 候选，传输/监听结束后 pfree 原候选 | 候选与实际端口一致；记录释放 |
| sender bind 冲突 | 两端 `file_transfer_mode()` | 关闭失败 socket，registry release，bind/connect port 0；正文和 ACK 不变 | bind 10013/10048，逐字节文件相同 |
| sender connect 阶段本地冲突 | 两端 `file_transfer_mode()` | 同上；后续重试不再反复使用失败固定端口 | connect 10013/10048，实际源端口变化 |
| receiver listener bind 冲突 | 两端 `file_transfer_mode_recv()` | 新 socket bind(0)，公告实际 listener 端口；pfree 原候选 | 请求方收到真实端口并完成文件传输 |
| relay upstream listener bind 冲突 | server `_forward_relay()` | bind(0)，公告实际端口，finally pfree 原候选 | 真实 relay resume 完成，原候选释放 |
| temporary server listener | 两个类 `create_temporary_server()` | palloc 候选经统一 bind；失败回退并返回实际端口 | stop 后 socket 和逻辑记录都释放 |
| temporary client palloc 分支 | client `create_temporary_client()` | palloc 候选经统一 bind/connect；返回实际 socket | side-channel 收发真实数据 |
| 控制连接固定端口 | `TCP_Client_Base.connect()` | 不调用 palloc；使用同一冲突识别和专用 wait/reconnect | fixed bind/connect 10013；None/0 不回退 |
| relay target 无固定源端口 | `_forward_relay()` target connect | 不改变；无 palloc lease | 原有转发/失败清理回归 |
| 主 server listener | `start_TCP_Server()` | 不改变 | 既有启动/停止测试 |
| 非端口冲突、远端拒绝、timeout | 所有路径 | 继续现有异常、等待和最大重试语义 | 原错误路径断言/真实失败 smoke |

## 4. 原子实施拆分

### A：内部接口与契约

- 文件：`connect_tcp.py`、allocator 单测、Port Allocation 文档。
- 意图：新增统一 socket 打开/替换/实际端口返回入口；不迁移业务调用点之外的行为。
- 预估：150–250 行。
- 验证：helper 的 bind 成功、端口 0、10013/10048 回退、非端口错误、registry callback 顺序；现有 `palloc/pfree` 契约测试全部保留。

### B：临时/监听路径迁移

- 文件：`connect_tcp.py`、`test_tcp_file_transfer.py`、必要的 temporary API 测试、Network API 文档。
- 意图：迁移 temporary server/client、两端 file receive listener、relay upstream listener；删除旧 listener 专用 helper。
- 预估：150–300 行。
- 验证：真实 loopback listener 公告、OS 实际端口、manual allocation 记录释放、stop/error 清理；覆盖 server/client 两方向。

### C：发送/控制路径收口与最终验收

- 文件：`connect_tcp.py`、`test_tcp_file_transfer.py`、`test_tcp_transfer_resume.py`、报告及文档收尾。
- 意图：迁移两端 file sender 的 bind/connect 回退；控制连接复用冲突判定但保留专用重试生命周期；删除重复分支。
- 预估：150–300 行。
- 验证：固定源端口 bind/connect 10013/10048、控制连接固定/None/0、真实文件/续传/relay、失败 socket registry 无残留。

每个原子阶段单独局部验证；不创建提交、不合并、不推送，等待人工审查后进入下一阶段。

## 5. 验收命令与运行时证据

```sh
uv run pytest \
  test/unit/network_api/test_TCP_server.py \
  test/unit/network_api/test_TCP_client.py \
  test/integration/network_api/test_tcp_broadcast_alloc.py \
  test/integration/network_api/test_tcp_file_transfer.py \
  test/integration/network_api/test_tcp_transfer_resume.py -q
```

必须提供的运行时证据：

1. 自动模式真实 bind：`palloc() == 0`，socket 实际端口非零，`pfree(0)` 不改变行为。
2. 手动模式真实 bind：候选端口被 OS 允许时，实际端口等于候选；socket 关闭后原记录从 allocator 中消失。
3. 手动候选被占用/注入 10013：实际端口不是候选，listener 公告实际端口或 sender 完成连接；原候选仍由 pfree 释放。
4. connect 阶段本地冲突：失败 socket 已从 registry 释放；替换 socket重新注册；最终 transfer/relay ACK 成功。
5. 两个并发 transfer：候选分配不重复，清理后所有逻辑记录归零；不以“测试不抛异常”作为证据。
6. Windows CI：Linux 只能注入 `winerror=10013` 验证分支；必须明确记录真实 Windows WSAEACCES/Hyper-V/WinNAT 未由本机证明。

## 6. 风险与明确未覆盖

- 最大风险是 socket 替换时先 release registry、后注册新 socket的时序；必须保持 cancel/close 竞态安全。
- `palloc/pfree` 当前是范围记账，不是 OS 端口锁；本计划不宣称它能消除跨进程 TOCTOU。
- 不迁移主 server listener 和无固定源端口的 relay target connect；这些路径的端口语义不同。
- 不改翻译 `.po` 文件；源 `.rst` 是本次接口契约来源，翻译生成流程不在本任务范围。
- 若人工要求把 `palloc()` 本身改为返回 socket/lease，需要另开破坏性公共 API 迁移计划，本计划不覆盖。

## 7. 规则与文档收尾

- 更新 `Port_Allocation.rst`：palloc 是逻辑候选分配；真实可用性由内部 socket 接口确认。
- 更新 client/server API 文档：`0`、固定端口、实际 `getsockname()` 端口和回退边界。
- 报告逐路径列出覆盖/未覆盖、实际命令和平台限制。

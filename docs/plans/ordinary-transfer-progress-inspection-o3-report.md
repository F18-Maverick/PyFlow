# O3 报告：Server/Client 本地只读进度查询

状态：实现、局部回归、独立真实 API/控制台烟测与完整相关套件均完成；待人类审查。只提交 O3，不自动进入 O4 或原 FID 大计划更改 4–6；未创建 git 提交、未合并、未推送。

## 提交意图与批准范围

人类明确“审计通过，开始更改03”。本项只增加 Server/Client 同名本地查询 API 和 `/transfers` 控制台命令：

```python
get_transfer_progress(*, include_persisted=True) -> dict
```

不增加远程查询协议、HTTP 端点、Web 字段、删除接口、僵尸判定、恢复触发或状态写回。O1/O2 的发送和接收生命周期保持。

## 改动内容

| 文件 | 关键变更 |
|---|---|
| `PyFlow/network_api/connect_tcp.py` | 新增共享 JSON-ready 观察格式化和磁盘读取包装；Server/Client 各暴露 `get_transfer_progress`; 两端本地 console 分别拦截 `/transfers`，参数错误仅打印用法 |
| `test/unit/network_api/test_transfer_progress_query.py` | 新增 Server/Client 参数化契约：作用域、运行时状态、内存/磁盘分离、relay 结构、错误状态、只读返回、副本隔离、关闭进度和 speed 开关 |
| `test/unit/network_api/test_TCP_client.py` | Client 实际交互循环 `/transfers` 输出与参数错误回归 |
| `test/integration/network_api/test_tcp_broadcast_alloc.py` | Server 实际 console `/transfers` 输出与参数错误回归 |
| `docs/source/File_Transfer/File_Transfer.rst` | 新增本地查询契约、字段与不变式 |
| `docs/source/Network_APIs/TCP_Server_APIs.rst` | 增加 Server API 和 console 命令说明 |
| `docs/source/Network_APIs/TCP_Client_APIs.rst` | 增加 Client console 命令说明 |
| `docs/source/changes/transfer-fid-routes.md` | 记录 O3 范围和限制 |
| 计划与本报告 | 记录授权、路径和证据 |

## 返回契约与数据流

入口：本地 Python API 或本地 console `/transfers` → 复制现有内存快照与运行时状态 → 可选只读进度文件解析 → JSON-ready 独立结果 → 本地调用者/终端。

返回顶层字段：

```text
{
  "progress_enabled": bool,
  "speed_enabled": bool,
  "memory": {
    "uploading": [...],
    "downloading": [...],
    "relays": [...]
  },
  "persisted": {
    "path": str,
    "status": "ok" | "missing" | "error" | "not_requested",
    "snapshot": {...} | null,
    "error": str | null
  }
}
```

文件 row 保留 `fid`、上传 `receiver`、逻辑 `peer`、`name`、`path`、`transferred`、`total`、`speed`、`active`、`socket_registered`、`paused`。完整上传 `(receiver, fid)` 作为作用域；相同数字 fid 不折叠。下载的 receiver 和 paused 为 null。

- 内存 rows：`active` / `socket_registered` / `paused` 是查询时本地观察值。
- 磁盘 rows：这些运行时字段均为 null，不伪装成 False。
- Server relay：originator、targets、completed，且只列权威路由。
- Client relay：既有恢复记录字段，不添加 Server 专用 completed 或 socket/lock 对象。
- `include_persisted=False` 不读取磁盘；默认读取但不调用 `_load_transfer_progress`，不恢复到内存。
- 缺失、空 JSON、损坏/旧格式、I/O 错误和跳过读取分别表达；错误不隐藏内存结果，也不修改文件。
- 返回结构不包含 socket、lock、Event 或可修改的内部 list 引用；查询不读文件内容、不计算 hash、不连接、不发送、不持久化。

锁顺序：在 `_transfer_progress_lock` 内复制快照、活动 fid 与 socket keys；释放后单独读取暂停条件变量和磁盘；不在路由/暂停锁内做文件 I/O 或打印。结果不是跨状态源的事务快照。

## 改前/改后回归

新增局部命令：

```bash
.venv/bin/python -m pytest test/unit/network_api/test_transfer_progress_query.py test/unit/network_api/test_TCP_client.py test/integration/network_api/test_tcp_broadcast_alloc.py -q
```

结果：**49 passed，0.25s，退出码 0**。

覆盖：

- Server/Client 两种实例的相同契约；
- 同号不同 receiver、上传/下载同号的作用域隔离；
- active、socket_registered、paused 的本地状态；
- relay 权威字段和 Client 私有恢复字段；
- 当前内存与落后/磁盘独有 row 分离；
- missing、empty、malformed、unsupported、permission/read error、not_requested；
- 返回对象和嵌套 row 修改不会改变内存、磁盘或 route；
- progress/speed 关闭时已有记录仍可见，速度变为 null；
- Client/Server 实际交互循环输出完整 JSON，日志关闭仍可见，错误参数不发网络。

测试文件单独 `ruff --select F,E9` 已通过。现有 `test_tcp_broadcast_alloc.py` 的 `threading`/`time` 未使用告警是存量测试问题，本项不改无关导入；生产文件另有既有 console `response` F841。

## 独立真实运行时证据

一次性脚本：

```bash
.venv/bin/python /tmp/pyflow-o3-smoke.py
```

退出码 0，输出 `O3_SMOKE_COMPLETE`，8.09s。脚本以真实 Server/Client、真实 TCP 文件端口和既有转发入口构造状态；不直接调用永久测试函数。明文、两个本地 console、离线实例及并发查询路径均实际运行。

### Server console 实例

观察到：

- memory 中同一 fid 的 upload/download 独立列出；upload row 的 receiver、peer、active=true、socket_registered=true、paused=true；download row 的 receiver/paused 为 null，active/socket_registered=true。
- Server relay 展示 originator、targets、completed；不出现 path 等 Server 未保存的副本。
- persisted status 为 `ok`，只含实际已持久化的 relay；当前 memory 中的活动普通传输未被查询写回磁盘。
- `/transfers --unknown` 和未闭合引号参数均输出 `Usage: /transfers`，没有发送网络命令。
- 日志关闭时仍输出 JSON。
- 查询前后内存 snapshot、进度文件 SHA-256、文件内容 SHA-256 均不变；原对象未加载磁盘 row。

### Client console 实例

观察到：

- 同号 upload/download 独立列出；完整 receiver scope 保留。
- retained forwarding upload 同时出现在 memory upload 和 client relay；磁盘 snapshot 独立保留该 row，运行时 active/socket/paused 为 null。
- Client relay 不被伪装成 Server route；未输出 completed 或 send_lock。
- `/transfers --unknown` 和未闭合引号同样仅打印本地 usage；日志关闭仍输出 JSON。

### 离线和并发

- 离线实例仅载入复制的进度文件：memory 为空，persisted relay/rows 可见；没有调用恢复入口或建立连接。
- 查询返回对象被修改后，原内存 snapshot、进度文件和传输文件 hash 保持不变。
- 两端实际传输暂停后循环查询 API 190 次，随后解除暂停；两条真实传输均返回 True，文件内容相同，owning 进度记录清理。查询期间没有 socket listener 收到 `/transfers`，没有状态回写。

Smoke 临时目录由 `TemporaryDirectory` 管理；报告完成后删除脚本。

## 编译、静态检查与完整相关套件

```bash
.venv/bin/python -m py_compile PyFlow/network_api/connect_tcp.py test/unit/network_api/test_transfer_progress_query.py test/unit/network_api/test_TCP_client.py test/integration/network_api/test_tcp_broadcast_alloc.py
ruff check --select F,E9 PyFlow/network_api/connect_tcp.py test/unit/network_api/test_transfer_progress_query.py test/unit/network_api/test_TCP_client.py test/integration/network_api/test_tcp_broadcast_alloc.py
.venv/bin/python -m pytest test/unit/network_api test/integration/network_api/test_tcp_transfer_resume.py test/integration/network_api/test_tcp_file_transfer.py test/integration/network_api/test_crypto_tcp.py test/integration/test_forward_extension.py test/integration/test_forward_msg_attribution.py test/integration/network_api/test_tcp_async_server.py test/unit/transfer_web/test_web_transfers.py -q
```

编译已通过。新增查询测试文件单独 `ruff --select F,E9` 已通过；联合 F/E9 检查保留既有 `connect_tcp.py` console `response` F841 和既有 broadcast test 未使用导入，本项未修改无关存量问题。

完整相关套件结果：**264 passed，282.21s，退出码 0**。包含新增查询契约、O1/O2 传输回归、普通双向文件/目录、恢复、加密/公钥、转发、异步和现有 Web 消费者；定向测试属于该总数，不重复相加，也不挪用 O1/O2 历史数量。

一次性 smoke 脚本及临时目录已删除；只保留永久回归、文档和本报告。

## 明确未覆盖与风险

- O4 `delete_config` 未实施；不支持按名删除、僵尸自动判定或进度清理。
- 查询不是一致性事务；内存、运行时开关与磁盘可能来自不同瞬间。
- 100% 字节、active=false、无 socket、speed=null 均不被解释为僵尸或完成。
- 不查询其他实例/进程，不保证共享路径独占，不读取源文件/接收文件内容。
- 不改变 Web `/api/transfers`，不做远程查询，不解决 ACK 应用层收据窗口。
- 未进行跨平台、负载、全仓测试或文档构建；完整相关套件只覆盖报告命令列出的范围。

## 人工闸门

O3 证据完成后交付人类审查。未获明确放行不进入 O4，不创建 git 提交，不合并，不推送。

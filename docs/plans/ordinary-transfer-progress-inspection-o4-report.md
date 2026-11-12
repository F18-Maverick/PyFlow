# O4 原子报告：按名删除本地配置与主动放弃持久化进度

状态：实现、行为回归、独立真实程序与完整相关套件均完成，待人类审查。O1–O3 已获审计通过。本项是补充计划 O4，不是原 FID 大计划的原子更改 4；未创建 git 提交、未合并、未推送。

## 提交意图与批准范围

人类明确“审计完毕，开始O4 delete_config更改”。Server/Client 增加同名公开 API：

```python
delete_config(config_name=None, *, is_list_config=False) -> bool | list[str]
```

仅列举或删除实例直接管理的进度文件与四种消息/事件日志，不接受任意路径；删除整个进度快照后，旧对象不得再次保存/加载或重启。不增加僵尸判定、逐 fid 清理、远程命令、HTTP 接口或自动删除策略。

## 改动与文件范围

| 文件 | 内容 |
|---|---|
| `PyFlow/network_api/connect_tcp.py` | 共享白名单解析/删除；两端公开方法；启动/关闭状态追踪；持久化/加载的主动放弃闸门 |
| `test/unit/network_api/test_delete_config.py` | 两端参数化行为回归：列表优先、白名单、文件错误、生命周期与发布/删除竞争 |
| `docs/source/File_Transfer/File_Transfer.rst` | API 返回、删除范围、调用顺序及整份快照放弃的风险 |
| 两端 `docs/source/Network_APIs/TCP_*_APIs.rst` | 公共接口入口与使用边界 |
| `docs/source/changes/transfer-fid-routes.md` | O4 变更记录，不改写前序报告的历史结论 |
| 两份计划、本报告、补充项总报告 | 授权、验收、范围和最终人工闸门 |

原先拟改既有恢复集成测试；实施时复用其已有真实失败/恢复回归而未增加重复装配。新测试集中在删除 API 的行为边界，真实关闭/删除/新进程验证使用独立烟测。没有删除、跳过或弱化任何既有测试。

## 接口与数据流

### 列表模式

`is_list_config=True` 优先于名字参数，返回排序的 `list[str]`，没有目标则为 `[]`。仅检查白名单目标是否为已存在的普通文件，不改变运行状态、不创建文件、不调用关闭/刷盘。可在运行中使用；结果表示 API 允许操作，不保证随后具有删除权限。

### 按名删除

当前路径属性的 basename → 白名单准确匹配 → 生命周期与在途操作检查 → 必要的进度/记录锁 → 普通文件检查 → unlink → 返回 True，合法文件本来不存在返回 False。

白名单为 `file_trans_progress_file`、`messages_log_file`、`events_log_file`、`sent_messages_log_file`、`sent_events_log_file`。Server 默认进度名为 `server_file_trans_progress.json`，Client 为 `client_file_trans_progress.json`，四类日志默认名字相同。路径重定向后使用实际 basename；同名指向不同路径明确拒绝歧义。

无名、空名、未知名、路径、通配符、目录、符号链接抛 ValueError；删除名非字符串抛 TypeError。PermissionError 等文件系统异常直接返回给调用者，不报成功；不因文件内容损坏而禁止明确按名删除。

不触及文件传输内容、密钥/TOFU、协议表、端口锁、启动器/Web 配置、数据库。列表不是目录扫描；调用者提供的名称不会被拼接成文件路径。

### 关闭与防复活

- 仅未启动或明确完成 stop/close 的静止实例允许删除。启动中、关闭中、启动过但未明确关闭、仍有 active fid/socket/relay 时拒绝。
- 生命周期包装器只在进度锁内更新短状态；不在该锁内执行整个阻塞连接/接受/关闭过程。既有 `_stopped` 及网络行为保留。
- 进度删除与 persist/load 共用 `_transfer_progress_lock`，等待已经开始的快照 publication 完成后再删除。
- 进度成功删除或合法目标已缺失时设置仅内存的 `_progress_discarded`。之后本对象 persist/load 分别提前返回、返回 0；旧对象启动入口在任何 socket 操作前明确抛 RuntimeError，要求新实例。
- 不清空原内存进度/路由，不重置 FID 分配器，不新增 JSON 字段或磁盘 tombstone。只使旧状态不能再用于运行或恢复。
- 日志删除锁住对应 record store，并拒绝仍有待刷记录的日志；不会建立永久日志禁写标志。重复关闭无新记录时不会重建日志。其他写入者及迟到控制 worker 应先结束。

## 定向回归与静态证据

```bash
.venv/bin/python -m pytest test/unit/network_api/test_delete_config.py test/unit/network_api/test_transfer_progress_query.py test/unit/network_api/test_TCP_client.py test/unit/network_api/test_TCP_server.py -q
```

结果：**101 passed，2.24s，退出码 0**。

覆盖两类实例的列表优先、运行中只读、逐个删除五种文件、默认参数与类型错误、未知/路径名称、目录/符号链接、重定向歧义、删除权限异常不损坏原文件或禁用默认保存、重复关闭、迟到 finish/load/persist、删除缺失进度、关闭未完成、活动 fid/socket/relay、未刷日志及发布/删除互斥。进度被其他写入者重新创建时，旧对象也不会读回或覆盖它。

```bash
.venv/bin/python -m py_compile PyFlow/network_api/connect_tcp.py test/unit/network_api/test_delete_config.py
ruff check --select F,E9 PyFlow/network_api/connect_tcp.py test/unit/network_api/test_delete_config.py
ruff check --select F,E9 test/unit/network_api/test_delete_config.py
```

编译退出码 0。联合 Ruff 退出码 1，仅原控制台 `response` 未使用（F841，当前行 6959），不改无关存量代码、不宣称完整 lint 通过。新增测试单独 F/E9 输出 `All checks passed!`，退出码 0。

## 独立真实运行时证据

```bash
.venv/bin/python /tmp/pyflow-o4-smoke.py
```

结果：正常退出、`O4_SMOKE_COMPLETE`，**50.71s**。脚本使用独立临时目录，实际启动 Server/Client，双向发送消息、事件和文件；仅在最终 ACK 边界注入 False，产生真实的上传/接收保留快照，而非手工构造期望 JSON。

路径矩阵：明文/加密 × 上传/下载 × 默认关闭保留/主动删除，共 8 组双端场景。

| 路径 | 观察 |
|---|---|
| 运行中列表与删除 | 列表优先于传入的非法类型名字，文件字节和 running 保持；删除抛 RuntimeError |
| 意外控制断线 | Client 已 running=False 且 receive worker 结束，但未显式 close；删除仍被拒绝 |
| 默认关闭对照 | 结束全部 socket worker 后，两端各自快照在新进程中恢复 1 行，原 fid 存在 |
| 主动删除 | 两端各列出确切的进度文件 + 四类日志；删除进度不改变任何未选择日志；逐个删日志不改变其他文件 |
| 重复关闭/迟到调用 | 删除后 persist、load、重复 close/stop 不重建快照；再次删除返回 False；旧对象 start/connect 明确拒绝 |
| 新进程读取删除结果 | 删除后的两端快照各恢复 0 行，没有原 fid |
| 新实例 | 复用原配置位置新建对象后，真实文件成功发送；另一次 ACK 失败的新任务照常持久化，读回只含新 fid |
| 真实权限失败 | 目录 chmod 0500 后，实际 unlink 抛 PermissionError，原文件字节不变；未 mock 文件系统错误 |
| 内容边界 | 源文件与完整接收文件内容相同，清理前后保持 SHA-256；未删除传输内容 |

新进程读回合计 32 次：16 次关闭后读取（含对照和删除前），8 次删除后读取，8 次新对象失败任务读取。4 组主动删除的双端场景均成功，此外 4 组默认保留场景保持原有恢复能力。加密场景完成真实握手并核对 `enc:` 文件连接标记；未声称本项进行了全路径抓包审计。

例：加密下载主动删除组的原 fid 为 9545167982；Server 与 Client 删除前均恢复 1 行、删除后均为 0，源/接收内容 SHA-256 保持 `acd6d25a759648fd2195f328b1d4151f15061d076eaf7c2540e2d5edaa38ab07`。随后新对象成功传输 fid 8974593928，并将新失败任务 fid 13269561224 正常保存读回。

## 完整相关回归

```bash
.venv/bin/python -m pytest test/unit/network_api test/integration/network_api/test_tcp_transfer_resume.py test/integration/network_api/test_tcp_file_transfer.py test/integration/network_api/test_crypto_tcp.py test/integration/test_forward_extension.py test/integration/test_forward_msg_attribution.py test/integration/network_api/test_tcp_async_server.py test/integration/network_api/test_message_event_store.py test/integration/network_api/test_send_message_store.py test/unit/transfer_web/test_web_transfers.py -q
```

结果：**332 passed，253.72s，退出码 0**。包括 O1/O2 真实 ACK 回归、O3 查询、两端关闭/连接竞态、普通文件/目录、转发恢复、加密公钥、异步、消息/事件存储及 Web 消费者。101 项定向回归与该总数有交集，不相加为唯一用例数量。

## 未覆盖、风险与人工闸门

- 整份快照删除会放弃正常中断任务及 relay 恢复关系，不只“僵尸”。用户自行核对两端 O3 输出后选择删除；对端不联动。
- 共享路径不是实例私有文件。必须停止所有使用同一路径的实例/进程；没有跨进程所有权/隔离，不抵御调用者同时修改实例路径属性或外部文件替换。
- 进度放弃不清空旧内存，仅封闭旧对象恢复/重启。需要继续运行必须新建对象；日志没有永久删除策略，新消息可以形成新日志。
- API 不主动关闭/join 传输 worker，不改变网络 teardown 流程；正在关闭的 server 接受循环或活动传输未退出时仍会拒删。
- 未改 ACK 应用层收据窗口、client_port 依赖、端点迁移、kind/tfid、现行恢复线路、ID 分配权、CI/依赖/覆盖率。
- 没有新远程协议、HTTP 或控制台删除命令；未做全仓测试、全量 lint、文档构建、跨平台或负载验收。

规则沉淀为行为回归：列表不能删；名称不可逃逸白名单；正在运行/关闭的对象不能删；明确放弃后的旧对象不能重写、加载或启动。O4 现提交人类审查，补充项总报告不等于自动通过总验收或允许合并/推送。原 FID 计划更改 4–6 仍等待单独批准。

## 证据收尾

一次性 `/tmp/pyflow-o4-smoke.py` 已删除；其自建临时目录在 `TemporaryDirectory` 成功退出时清理。保留永久行为回归及本报告，不保留临时测试装配。补充 O1–O4 的总体路径对照和未覆盖风险见 `ordinary-transfer-progress-inspection-report.md`。

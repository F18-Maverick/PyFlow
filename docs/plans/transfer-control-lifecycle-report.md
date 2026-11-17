# 传输控制生命周期实施报告

## C1：实际控制会话取消与旧 owner 收尾

状态：**C1 实现及局部验收完成，等待人工审查**。V1 最终通过，独立真实 socket 场景通过；此前失败及修正记录保留在下文。C2–C6 未开始，未创建 git 提交、合并或推送。依据：[需求卡](transfer-control-lifecycle-requirements.md)、[计划 C1](transfer-control-lifecycle-plan.md#8-原子拆分固定次序逐项人工审查)。

### 意图、更改路径与解决的问题

- `PyFlow/network_api/connect_tcp.py`：删除 Server/Client 两份 `_drop_stale_transfers`。重连不再按 peer 扫描进度表并关闭扫描时的当前 socket，避免迟到清扫误伤新会话。
- `_retire_transfer_session` 统一取消实际控制 socket 的握手、listener/data socket、控制状态及未绑定接收请求；资源登记保留至 worker 真正收尾，`wait_socket` 等待所属资源释放。关闭 socket 在 registry 锁外执行。
- `_sending_transfer`、`_start_resume_send` 保存准入时控制 socket；`_wait_transfer_session` 仅等待捕获的原 send state/receive record。旧 finally 继续按对象身份释放，不删除替换后的 owner。
- Server 在会话退役时保存旧 owner 快照，后台排空后删除快照；重连 worker 固定新准入 socket，旧 worker 不再按 peer 取得后来替换的连接。`stop` 在 client 锁外取消和关闭连接。
- Client 先退役原 socket，再启动新控制 reader；后台恢复等待原 owner，不再在 reader 启动前等待。连续重连的恢复 worker 按原线程链排空。转发恢复显式传入捕获的 `control_socket` 到 `_forward_one`，避免旧任务借新 socket 发握手并形成等待环。
- `test/unit/network_api/test_transfer_handshakes.py`、`test_resume_matching.py`：保留实际会话取消、同 fid 占用和旧 owner 等待契约，增加旧清理后新 socket 仍可收发及迟到释放不删新 owner 的断言。
- `test/integration/network_api/test_tcp_transfer_resume.py`：将原 sweep 用例迁移为实际控制会话退役；相同 fid 的不同会话保持独立；暂停腿退役后用原 fid 真实恢复，比较内容并确认发送端成功清理。
- `test/integration/network_api/test_tcp_transfer_control.py`：事件固定“旧转发恢复 worker 已准入但尚未执行 → 新 connect → 放行旧 worker”，覆盖明文别名、加密 thread、加密 async/wildcard/tracking off。断言旧任务仅使用原 socket、失败保留原上传行、新会话可继续收消息。

数据流：实际控制会话退役 → registry 标记关闭并取消所属资源/握手 → 清除所属运行时控制状态 → 捕获旧 owner → 后台等原资源及 owner 收尾 → 经固定的新会话发恢复请求 → 原 fid/路径恢复 → 真实最终 ACK → owning 清理。

### 局部验证

V1 最终结果：**112 passed、97 deselected，240.29 秒，退出码 0**（Linux、Python 3.10.20）。筛选范围与计划相同；97 项为既定 `-k` 未选中，不是新增 skip，也不代表全套通过。

失败记录：首轮输出包含 2 个 fixture 错误，300 秒超时，不能记为通过。窄定位运行得到 17 passed、1 error：新增加密重连用例未等待新握手完成便结束，teardown 发现 `_crypto_exchange_thread`、`_crypto_wait_server_ready` 尚未退出。为新控制连接双方补上真实 `_wait_control_secure` 就绪前提，保留原零存活 worker 断言；随后执行下面完整 V1 选择并通过，未修改生产加密流程或延长 fixture join 来掩盖错误。

```bash
.venv/bin/python -m pytest test/unit/network_api/test_transfer_handshakes.py test/unit/network_api/test_TCP_client.py test/unit/network_api/test_TCP_server.py test/unit/network_api/test_resume_matching.py test/integration/network_api/test_tcp_transfer_control.py test/integration/network_api/test_tcp_transfer_resume.py -k 'disconnect or reconnect or session or owner or stale or cleanup or resume' -x -vv --tb=short
```

独立程序不调用 pytest 测试函数，使用真实控制/data socket、加密、文件 I/O 和最终 ACK；Event 固定交叉时序，方法包装仅观察真实执行或设置旧 worker 放行点。临时脚本 `/tmp/pyflow-transfer-lifecycle-smoke.py` 已在验证后删除，运行时临时文件与密钥目录也已清理。

```bash
# 每项分别运行 --alias、--encrypted、--encrypted --async-server --untracked
PYTHONPATH="$PWD" .venv/bin/python /tmp/pyflow-transfer-lifecycle-smoke.py --case direct-leg <参数>
PYTHONPATH="$PWD" .venv/bin/python /tmp/pyflow-transfer-lifecycle-smoke.py --case relay-leg <参数>
```

- S1 会话部分：3 组参数各完成上传、下载，共 6 次同 fid 恢复。fid 为 810/811；实际原文件 offset=0，原完整路径保持，旧 control/data socket 均已关闭，新控制端点保持，发送端实际最终 ACK=True，成功后所属进度行删除。加密 async 使用 wildcard 绑定，并在产生保留行后关闭 tracking 再断线/恢复。
- S1 完整内容 SHA-256：`f2a6736d846749945c57b953b657f9b483c8c4e842b590e00a0cdd0574c20c33`。明文、加密 thread、加密 async 的独立程序退出码均为 0；各自 35、74、73 个观察到的 worker 最终存活数为 0。
- S2 会话部分：3 组参数均证明旧转发恢复 fid=820 的 worker 固定原 socket、返回 False、原上传行保持且不建立伪 route；新控制连接继续完成一次真实转发，最终 ACK=True、内容一致。新转发按原规则分配自己的 fid，不把该新任务报告为 fid=820 的恢复成功。
- S2 内容 SHA-256：`5bc85cfbbe30eb61c8fcbbacdd78975ce49973c0fbf69771c03258ac793c7c03`。3 组程序退出码均为 0；各自 23、62、62 个 worker 最终存活数为 0。
- 独立程序首轮加密 S1 的观察器误将公钥传输（fid=None）的 ACK 计为业务恢复结果，断言失败、退出码 1；按业务 fid 过滤观察后，只重跑受影响的加密 S1 参数组并通过，未以此修改或绕过生产传输/加密路径。

### 范围、不变量与未覆盖项

- C1 对应 P01–P07、P16/P19/P20 的会话生命周期和回归保护。独立程序不穷举全部组合；V1 实际通过了 online resume 的各方向/加密/async/tracking off、文件/文件夹失败后恢复、实际落盘 offset、进度保存/重载、EOF/ACK、稳定转发 route/child、旧 socket 握手隔离及 localhost/wildcard 身份回归。不能把持久化重载用例说成已完成独立多进程重启矩阵，也不将零 offset 独立程序当作所有恢复路径的替代证据。
- 不改变稳定 upstream/child fid、pause/start 四 token、端口响应、`file_transfer_mode` 签名、进度/快照 schema、普通 busy 规则或真实 ACK 判成功规则。未改 CI、依赖、coverage 策略，未删用例、跳过路径或弱化断言。
- 问题二继续暂缓：同一控制 socket、同 fid 换腿时迟到 pause/start 的严格代次隔离未实现；未新增 leg_id、换新 fid、协议字段或自动重试。
- C2 全局暂停、C3 route 定向重建/offset 查询/逐 Target 顺序恢复、C4 EOF 补确认、C5 自转发拒绝、C6 总体验收未实施，不能据 C1 结果宣称这些行为已完成。
- 未运行 native Windows、其他 Python 版本或跨进程并发进度写入验收；本轮未做抓包，不将加密真实传输等同于全协议安全审计。
- 本轮会话生命周期契约回归作为规则沉淀；变更记录见 `docs/source/changes/transfer-fid-routes.md`。C1 现等待人工审查，不自动进入 C2。

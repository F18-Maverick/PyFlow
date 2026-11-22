# 原生 TCP 文件转发 — A1 阶段报告

**状态：A1 已实现并完成局部验证，待人工审查；未开始 A2，未创建提交。** [更改计划](forward-one-to-one-plan.md) · [需求卡](forward-one-to-one-requirements.md)

## 意图、范围和审查点

让同一 Server 上所有原生转发（首次或恢复，单目标或多目标）共用一个严格的待下发字节上限。改变的是既有 `max_mem_buff` 的作用域，不是转发任务的身份、协议或用户暂停状态。

| 文件 | 本阶段改动 |
|---|---|
| `PyFlow/network_api/connect_tcp.py` | 在 Server 实例上创建 `_ForwardBudget`；`_forward_pump` 改为先计费再发布、逐下游归还。实际控制会话退役关闭所属数据 socket 后唤醒等待额度的 reader。 |
| `test/unit/network_api/test_transfer_control.py` | 用已满队列触发发布失败：只退本次预留，不退其他发送持有的字节；空出的额度可再用。 |
| `test/integration/network_api/test_tcp_file_transfer.py`、`test/integration/network_api/test_tcp_transfer_control.py` | 将两处要求越过水位的压力断言改为实际扣住 writer、证明 reader 等待且总账不越界；增加跨源、跨路由测试，保留控制状态、真实内容、ACK 和清理断言。 |
| `docs/source/File_Transfer/File_Transfer.rst`、`docs/source/Network_APIs/TCP_Server_APIs.rst`、`docs/source/changes/forward-one-to-one.md` | 说明按下游份数计费、适用的 Server 版本和非 RSS 边界。计划中更新了授权闸门。 |

**数据流（P5、P4 的资源分支）：** reader 从原上传连接读文件头和正文，不预占下一次读取的额度。对于每个下游，只在总账有空间时发布容得下的字节；一块或一次扇出的总量大于上限也可分段推进。writer 出队不退账，沿原 `send_message` 发送，返回或抛错才归还当前项。失败的 writer 先在预算锁下摘除自己的队列，再在锁外排空并归还剩余字节。没有下游的 EOF 补确认仍校验头，但不入队、不争额度。

队列成员检查、预留与发布受同一个 Condition 保护；每次被唤醒都重新核对 socket 和队列。预算锁不跨网络读写、用户控制或路由锁。用户 start 不能制造额度，退还额度也不发 start。任务仍须得到所属目标的真实确认；活跃 pump 清理与失败路由保留用于恢复是两件事。

## 局部验证与实际证据

环境：Linux x86-64，CPython 3.10.20，pytest 9.1.1，本机 IPv4 TCP。

**先红后绿。** 旧实现执行 `.venv/bin/python -m pytest -q test/integration/network_api/test_tcp_file_transfer.py::test_forward_flow_control_pauses_uploader`，退出 1：限制 **32,768 B**，一个真实 writer 尚未返回时已计费 **65,555 B**。实施后执行计划 V1：

```bash
P=.venv/bin/python; U=test/unit/network_api; I=test/integration/network_api
"$P" -m pytest -q "$U/test_transfer_control.py" "$I/test_tcp_file_transfer.py::test_forward_flow_control_pauses_uploader" "$I/test_tcp_file_transfer.py::test_forward_shared_budget_across_routes" "$I/test_tcp_transfer_control.py::test_relay_pressure_preserves_user_intent_and_source_start_cannot_open_reader"
```

退出 **0：145 passed in 64.08s**。32 KiB 用例保留了 300 KiB 正文；跨路由用例还将额度压到 1 B：双目标路由的 writer 持有这一字节时，另一源的单目标路由连头都不能发布。释放 writer、仍扣住第一源的正文时，另一源收到最终 ACK；第一条路由随后向两个真实目标完成交付。

**独立 S-pressure。** 执行 `.venv/bin/python /tmp/pyflow-forward-one-to-one-smoke.py --case pressure`，退出 **0**。每组使用两个不同来源、两个文件、一个单目标和一个双目标路由；5 条控制连接，2 条上行、3 条下行数据连接。在真实 `send_message` 边界保持 writer 未完成，不替换握手、加密、文件、端口、数据或 ACK。表中峰值为运行时采样和预留总账峰值，两者相等；正文仅做逐字节比较，不写入日志。

| 路径（Server / 通道 / 端口） | 上限 / 峰值 B | 两条 reader 进入额度等待的次数 | 两源获最终 ACK | 真实目标 ACK 数 | 核对成功正文 B |
|---|---:|---:|---|---:|---:|
| 放开 writer（线程 / 明文 / 系统） | 1 / 1 | 342、653 | 是、是 | 3 | 805 |
| 目标断线、实际下游写失败（asyncio / 加密 / 系统） | 512 / 512 | 3、1199 | 否、是 | 2 | 614460 |
| 等额度的源控制会话断开（线程 / 明文 / 手动） | 256 / 256 | 1144、1 | 是、否 | 1 | 307203 |
| 两条路由受压时停服（asyncio / 明文 / 系统） | 128 / 128 | 1、1 | 否、否 | 0 | 0 |

- 放开组发送的计费数据为 **856 B = 805 B 正文 + 51 B 文件头**。满额时显式 start 后 reader 再次进入等待，读取数不变；排空后被用户暂停的源仍未发下一块，另一源已完成。其他三组没有自动 pause/start。
- 源断线组：占满 256 B 的另一 writer 尚未放行，等待中的源已失败并退出 pump。停服组两个源都没有假成功。四组结束时总账、各 pump 余额、活跃 relay、额外分配端口均为 **0**，临时 socket/握手及传输 owner 登记已清理。失败任务不要求删除恢复路由。
- 脚本首次退出 1，未进入计费路径：测试把目标控制连接绑到另一个回环 IP，而文件 listener 未监听该 IP，真实连接返回 `Connection refused`。仅将测试拓扑改为同一回环地址、不同端口后执行上述四组，未绕过连接或改生产监听逻辑。

**P5 恢复补证。** `.venv/bin/python /tmp/pyflow-forward-one-to-one-smoke.py --case pressure --recovery-only`，退出 **0**。先真实断开下游数据 socket，97 B 文件仅落盘 11 B；随后从实际 offset 11、原 upstream FID `8171444276` 和 child `6337585112` 重放。另一来源持有全部 1 B 时，该恢复连文件头都在等额度。另一空文件的目标此前已真实 ACK、源漏收最终 ACK；其原 FID `12466411572` 在同一满额期间以 **0 下游、0 新计费**完成补确认。释放 writer 后两个剩余文件内容均一致，峰值 1 B，最后余额及活跃 relay 均为 0。一次性脚本不入库。

## 未覆盖与风险

- P5 和 P4 背压分支有上述证据；P8 只覆盖表中的配置及本机端口/临时资源。未运行 Windows、macOS、自由线程 Python 或所有配置的笛卡尔积，不把这些结果外推到其他平台。
- A1 不实现 Client 拆分、发送名额、排队项取消或一对一任务控制隔离；未执行 V2–V4 或全项目回归，未改 CI、依赖、配置格式、普通直传、消息、Web 或落盘扩展。
- 严格上限针对队列和 writer 手中的待交付字节，不覆盖 reader 当前块、加密临时对象或 OS 缓冲。没有带宽公平保证。部署依赖总额保护须更新 Server；只更新 Client 无效。下一闸门是人工审查 A1，不自动进入 A2。

# R2 报告：实际控制 socket + FID 握手闭环，移除 UUID request

状态：R2 已完成实现与本报告所列验证；最终受影响完整套件 **434 passed in 278.89s**，明文/加密独立真实程序通过，文档构建成功并保留两条既有警告。用户已明确授权在 R1 主要更改完成后继续 R2。当前停在人工审查闸门；未创建提交、未合并、未推送。不以此报告宣称人类已经审查通过。

## 1. 意图与范围

执行 `transfer-sender-fid-revision-plan.md` 的 R2，不恢复 FIFO、共享端口缓存、共享错误槽或额外尝试 token。保留 R1：每跳发送方分配新 FID，源端 upstream FID 预绑定现有五项上传行，Server 逐目标分配 child FID，`resume=0` 也明确走旧 route。保留进度/route JSON、原接收路径、逐目标完成状态、文件字节、既有最终数据 ACK 和加密入口。

R1 阶段的 427 passed / 1 项旧 `request_id` 调用失败没有通过给测试补旧参数来掩盖。用户要求 R2 删除该接口；当前 `forward_target_command`/`forward_item_to` 均不再有 UUID 参数。R1 阶段事实单独保留在 `transfer-sender-fid-r1-report.md`。

## 2. 当前线路与数据流

```text
/file <原字段> <fid>
/file_folder <原文件项字段> <fid>
/crypto_pub_key <fid>
/server_file_transfer_port <port> <fid> <stamp>
/forward_item <kind> fid=<upstream_fid> [resume=<offset>] <原文件与目标字段>
/forward_error <fid> <shell-quoted reason>
/resume_trans <fid> <offset> <origin_repr> <target_repr>
```

目录创建不申请文件端口。旧五 token UUID 端口响应被拒绝，没有 fallback 或兼容别名。`uuid` 仍可用于与握手无关的公钥临时文件名，未因名称相同删除无关用途。

1. 发送前按实际控制 socket 对象及 FID 登记，使用 `_send_transfer_request` 在真正调用既有发送入口之前标记可能已发出；每个登记只允许一次发送。
2. 正常端口或错误只交给该 socket/FID 的当前登记；端口和 stamp 是同一结果。无人等待、错误 socket/FID、已终结或未决 key 的响应被丢弃，不缓存到后续请求。
3. `wait`/`wait_many` 与取消在同一 condition 锁内决定结果和未决归属。未发送退出可重登；已发送且无终结结果的超时/取消/发送不确定 key 保留于 `_unresolved`，同一会话不再发送该 FID 的新协商。已明确收到结果的目标不因另一目标超时被一律标为未决。
4. 登记 record 是私有所有权句柄，不是新的线路或持久化 attempt ID；旧 context 的取消不能删除后来同 key 的新登记。普通与中继恢复沿用现有 per-upload 串行和同目标合并，不把所有文件全局串行。
5. 中继生产方在发送端口或错误前标记该协商已终结；端口之后的文件数据/ACK 失败只走原数据腿，不再发送第二个控制错误。原腿结束前不启动同 FID 第二条写入。

## 3. 控制会话与资源归属

- 待 accept 的 listener 在对外公告前登记到所属控制 socket；accept 后的数据 socket、公钥/普通出站 socket，以及中继上下游 socket 也登记。中继下游 socket 同时受目标与源控制会话约束。
- 控制会话自然断开、对象关闭或 Server 停止时，`cancel_socket` 只退役该 socket 的 waiters、未决 key 和所属 listener/数据 socket。关闭资源不要求另一条健康控制连接断开，未完成进度/route 保留。
- 接收循环捕获实际控制 socket；旧 Server 会话注销不能删除同地址的新 client/crypto 状态，旧 Client receive 循环不能把新会话的 running 状态清掉。新 socket 可按原 FID 恢复，旧回调不能满足新等待。
- 已消费端口后的数据/ACK 失败不等于未决端口，也不主动断开健康控制连接。暂停仍是原数据腿上的状态；控制会话关闭导致数据 socket 关闭后，普通发送和 relay 的暂停/背压等待能退出，不能挂住 worker。

## 4. 修改文件与不变边界

- 生产：`PyFlow/network_api/connect_tcp.py`，包括 registry、解析/构造、双向普通/目录/公钥/恢复发送、relay 两侧、公开 builder 与控制资源生命周期。
- 测试：`test/unit/network_api/test_transfer_handshakes.py`、`test_fid_routes.py`；`test/integration/helpers.py`；`test/integration/network_api/test_tcp_transfer_resume.py`、`test_tcp_file_transfer.py`。crypto、async、forwarding attribution 与 Web 既有消费者纳入最终受影响回归；没有为它们保留旧参数别名。
- 活动文档：File_Transfer、TCP 两端 API、transfer 三份活动变更说明、主计划及 sender-FID 修订方案。本报告和 R1 报告新增。历史 atomic-1–5/O 报告不回写；活动文档将被取代的旧 request/接收端分配说明明确标记为历史。
- 无 CI、依赖、coverage、进度值结构、JSON schema、加密算法或鉴权策略变更。不修改与传输握手无关的 Web 联系人/FTP request ID。
- Python LSP references 请求返回 `-32601 Unknown request: textDocument/references`，已记录工具问题；使用定向仓库检索迁移真实调用方，没有声称 LSP 引用枚举成功。

## 5. 实际验证

### 独立真实程序（不是调用 pytest 测试函数）

`.venv/bin/python /tmp/pyflow-r2-smoke.py` 启动真实 Server、源 Client 与两个目标；每实例日志、进度、公私钥及 TOFU 路径隔离到临时目录。分别运行明文与加密，最终两种模式全部断言通过：

| 路径 | 实际观察 |
|---|---|
| 双向普通/目录项 | source→Server 的 `nested/first.bin` 和 Server→Client 文件字节正确 |
| 两个不同目标并发 | 延迟第一 upstream 端口回复，第二先完成；60,017 / 70,029 字节各到正确目标 |
| FID 预绑定与线路 | 源控制命令发送前已有该 FID 的五项路径绑定；无 request 标记；所有 listener 回复四 token |
| 端口/stamp | 明文为 plain，加密为 enc:；文件通过对应数据通道完成 |
| 部分目标 ACK 丢失 | 源返回 False，原上传及 route 保留，另一目标完成集合保留 |
| EOF 恢复 | 请求 `resume=60017`，原 upstream/child FID 与原文件完成；成功后清理 |
| 未决超时 | 第一次 listener 回复被保留，等待超时；同 socket/FID 不能再登记或发请求 |
| 迟到端口/错误 | 注入原端口和 FID 错误响应，未解除未决 key |
| 健康控制连接 | 超时后另一 FID 完整传输成功；原控制 socket 仍存活，真实 `/time` 往返成功 |
| 自然断线与同 FID 新会话 | shutdown 原控制连接、释放旧 socket 后重连；原 FID 获新 listener，旧 socket 回调不能填充新等待，文件字节正确 |
| 资源收尾 | 明文 64 个、加密 116 个跟踪 worker 全部退出；旧 listener/资源随会话关闭 |

首次烟测曾调用终结对象的 `close()` 后再 `connect()`，按既有关闭语义连接失败；脚本改为真实控制连接 shutdown → 接收循环退出 → 释放 socket → connect 后通过。未为烟测改变生产 close 语义。它与最终成功结果分开记录。

烟测为临时验证程序，未保留在仓库或其原临时脚本路径。

### 受影响完整套件

```text
.venv/bin/python -m pytest test/unit/network_api \
  test/integration/network_api test/integration/test_forward_extension.py \
  test/integration/test_forward_msg_attribution.py \
  test/unit/transfer_web/test_web_transfers.py -q
```

首轮 R2 整合：**431 passed, 3 failed in 293.30s**。三个迁移缺陷为：把合法四 token plain 回复遗留在拒绝列表、双目标测试漏建 event/lock、两个乱序测试从 stamp 槽而非 FID 槽取值。已修正测试驱动，保留原乱序、内容/路径/ACK 断言；没有通过生产特殊分支、跳过或弱化断言规避。新增原 ACK-loss 场景断言每次只有一个控制端口结果且控制连接保持健康。

次轮完整运行：**433 passed, 1 failed in 280.82s**。失败为 `test_unified_resume_serializes_distinct_offsets_without_local_fid_fallback`：测试先从两个不同控制连接发送请求，却未经同步就要求第一个调用对应的目标先进入发送。实际日志显示第二个目标的 offset 先到达源端，并非发生并行写入或 FID 串任务。已利用该测试原有的最终 ACK 屏障，在发第二请求前等待第一目标到达屏障；不修改生产调度，不删除串行、offset、旧 FID、原路径、最终字节及无本地 fallback 断言。

修正后的最终完整运行：**434 passed in 278.89s**。包含本报告的单元、真实 socket 集成、加密/公钥、async、转发扩展/归属和 Web 消费者；没有失败或跳过。前两轮失败及修复原因保留如上，不用最后一次成功覆盖历史事实。

### 文档与静态检查

- `.venv/bin/python -m sphinx -b html docs/source /tmp/pyflow-r2-html`：27 个源文件构建成功，保留两条既有警告：配置的 `_static` 不存在；`command_control_extension_tcp` docstring 第 23 行 definition list 缺空行。
- `.venv/bin/python -m py_compile PyFlow/network_api/connect_tcp.py`：成功。
- `.venv/bin/python -m ruff`：环境未安装 ruff 模块；改用已安装的 `ruff` 可执行文件。
- `ruff check --select F,E9` 覆盖上述生产文件、helper 与相关单元/集成文件：修正测试迁移后只剩历史 custom-handler 的 `connect_tcp.py:7066 F841 response`。本轮未改该无关分支，不声明 lint 全绿。

## 6. 路径结论与剩余风险

| 修订计划路径 | 结论与实际证据 |
|---|---|
| 普通双向文件、目录项 | 通过；完整 file-transfer 套件及独立明文/加密双向程序，保持原嵌套路径和精确字节 |
| 双向公钥、并发 key exchange | 通过；完整 crypto 套件及独立加密 Server/三 Client 初始化与正常消息往返 |
| 首次一对一/多目标中继 | 通过；源 FID 预绑定、不同目标乱序、Server 仅分配 child 的真实回归与独立运行 |
| 无效目标、协商失败 | 通过；既有 forwarding/route 失败保留回归及两源同 FID 的定向拒绝，不覆盖另一个任务 |
| 普通/中继 resume | 通过；零 offset、部分 ACK、EOF 再确认、原 ID/路径/内容与完整目标集合 ACK 断言 |
| 同目标重复、不同 offset | 通过；重复请求合并及经 ACK 屏障确定性的 distinct-offset 串行回归 |
| 未决端口超时、取消、发送不确定 | 通过；registry sent/未发送取消分类、per-target wait_many 与双向真实超时隔离；独立程序证明另一 FID/控制命令仍工作 |
| 数据腿失败、EOF ACK 未完成 | 通过；失败保留进度/route；每次只有一个控制终结结果，原控制连接存活；独立程序原 ID EOF 恢复 |
| 控制连接自然断线 | 通过；双向旧 socket 回调拒绝、pending listener 退休、暂停数据 socket 关闭、新 socket 复用 FID；既有自动重连回归保留 |
| 源离线、目标重连 | 通过；完整 resume suite 的 pending 最新 offset、失败不提前删除与二次断线路径 |
| 占用冲突 | 通过；接收、活动 socket/FID、pause、relay/index 的 route 原子拒绝；跨源同值不覆盖，receiver 作用域不误合并 |
| 暂停/继续、O3/O4、读回 | 通过；现有 network_api 相关单元/集成回归，进度结构不增添持久化握手状态 |
| async、forwarding API、Web | 通过；完整 async、转发扩展/归属及 Web transfer 消费者套件，无旧 request 参数旁路 |

取舍按已批准方案保持：同会话未决 FID 可能长期等待自然重连，未决集合内存随未决 FID 数量增长，不宣称 O(1)；不为未决 FID 新增主动重连、自动改号、attempt token 或新重试协议，既有 `is_auto_reconnect` 配置不变。不承诺区分错误实现对已完成协商重复发送、且恰落在下一次同 socket/FID 等待里的旧响应；单终结生产约束是去 UUID 的前提。不同源相同 upstream 数值冲突明确拒绝而不是同时成功，不引入 source-scoped route/JSON 迁移。

不支持新旧节点混跑，不自动迁移旧进度，不解决 client_port 变化的身份迁移，不做中继落盘或暂停持久化。不以本次烟测声称完成全路径抓包、全局日志脱敏或安全审计。最终完成后交付人工审查，不自动提交、合并或推送。

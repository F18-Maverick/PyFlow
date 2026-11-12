# 原子更改 5 报告：一次性监听握手关联统一

状态：已实施并完成下列验证，待人类审查；未创建 git 提交、未合并、未推送；未进入原子更改 6。

## 1. 提交意图与范围

只落实主计划第 8 节：双向普通文件、目录内文件、公钥、每个中继目标 listener 和中继源上传，统一使用实际控制 socket 所属的一次性 request。删除旧端口缓存、专用中继上传响应、FIFO 和共享错误槽，不保留兼容入口。

不改变新传输 FID 的分配方、`tfid`/`relay_fid=`、稳定上下游 FID、五项进度行、Server route、文件字节格式、暂停/恢复、既有最终数据 ACK 或加密 framing。接收端分配权切换仍属于未放行的更改 6。

## 2. 数据流与公共线路

1. 发起方先生成全新 UUID hex，并登记 `(request_id, actual_control_socket, expected_fid)`，再发送命令。中继源上传尚无 upstream FID，只有该请求匹配后才能接收 Server 分配的 FID；没有改变分配权。
2. `/file`、`/file_folder`、`/crypto_pub_key` 在末尾 FID 前携带 `request=<uuidhex>`；`/forward_item` 在 kind 后携带该标记。目录创建的非传输命令不因此增加数据连接。
3. 接收方建立真实 listener，经同一控制连接返回唯一响应：

   ```text
   /server_file_transfer_port <port> <fid> <stamp> <request_id>
   ```

4. `_TransferHandshakeRegistry` 按实际 socket 对象、request 和预期 FID 匹配，将 port 与 stamp 作为一份结果交付唯一 waiter。错 socket/FID、重复、迟到和已取消结果直接丢弃，不缓存供后来请求消费。中继各目标独立登记，允许乱序响应。
5. 成功消费移除登记；超时、异常、取消、控制连接退出及对象关闭退役相应登记并通知 waiter。上下文管理的 `_transfer_handshake` 负责退出时清理。客户端接收线程捕获其所属控制 socket，而不是迟到时读取已经更换的 `self.client_socket`。
6. 删除生产 `/forward_upload` 命令、`_forward_upload_queue`、共享 `_forward_error` 槽以及旧 `file_server_port_list`/等待入口。中继源失败只用 `/forward_error <request_id> <reason>` 回到其实际控制 socket 所属请求。
7. 匹配后的原有文件数据连接、加密 attach、ACK 与进度/route 成功清理或失败保留继续使用既有实现。公钥文件腿仍 plaintext；request 不写入进度、route JSON 或恢复身份。

这是不兼容的线路切换，不支持新旧节点混跑，不提供旧响应 fallback。

## 3. 修改范围

- `PyFlow/network_api/connect_tcp.py`：request 构造/解析、统一 port 响应、瞬态 registry、普通/目录/公钥与中继两侧消费者、实际控制 socket 分派和断开清理；删除旧协调状态与命令。
- `test/unit/network_api/test_transfer_handshakes.py`：乱序、socket/FID/request 匹配、取消/超时/断开、定向错误、重复及旧/畸形响应拒绝。
- `test/integration/helpers.py`：低层数据传输测试先通过真实控制连接请求其唯一 listener，不再读共享端口缓存。
- 受影响真实调用方与回归：`test/integration/network_api/test_tcp_transfer_resume.py`、`test_tcp_file_transfer.py`、`test_crypto_tcp.py`、`test_tcp_async_server.py` 和 `test/integration/test_forward_msg_attribution.py`。保留最终 ACK、稳定 FID、嵌套目的路径与失败状态断言。
- 活动文档：`docs/source/File_Transfer/File_Transfer.rst`、`docs/source/Network_APIs/TCP_Client_APIs.rst`、`TCP_Server_APIs.rst`、`docs/source/changes/transfer-fid-routes.md`、主计划及本报告。历史原子报告不回写。
- 翻译清理按活动变更说明的清单，仅移除 19 个 catalog 中 54 个过期 gettext 块；未全局再生成、未提供新译文。涉及五种语言的 File_Transfer/TCP 两端 catalog，以及 ko/ru/zh_CN/zh_TW 的 connect_tcp API catalog。

## 4. 验证证据

### 4.1 受影响完整套件

```text
.venv/bin/python -m pytest test/unit/network_api \
  test/integration/network_api test/integration/test_forward_extension.py \
  test/integration/test_forward_msg_attribution.py \
  test/unit/transfer_web/test_web_transfers.py -q
```

结果：**413 passed in 308.30s**。范围含普通/目录、恢复/ACK、公钥/加密、async 控制通道、中继扩展和 Web transfer 消费者。

该轮之后补入旧会话回调隔离用例，单独执行：

```text
.venv/bin/python -m pytest test/integration/network_api/test_tcp_transfer_resume.py \
  -k reconnected_handshake -q
```

结果：**2 passed, 35 deselected in 4.94s**。这两例未计入前述 413；不是一次运行得到的 415。

### 4.2 定向乱序、过期与重连回归

```text
.venv/bin/python -m pytest test/unit/network_api/test_transfer_handshakes.py \
  test/integration/network_api/test_tcp_transfer_resume.py \
  -k 'handshake or expired_listener' -q
```

结果：**12 passed, 32 deselected in 8.17s**，执行时间早于新增的两例重连测试。

- `test_concurrent_forward_upload_handshakes_match_out_of_order_replies`：真实 Server 与客户端故意阻塞第一上传的 port 响应；第二上传先完成，再释放第一上传。两个源文件内容分别正确，两个调用均成功，进度与路由清理。它直接约束旧 FIFO 串任务问题。
- `test_expired_listener_reply_cannot_capture_a_new_attempt[upload/download]`：第一次 listener 请求超时后，第二次同 FID 请求先收到旧响应和错误 FID 响应，再收到自己的有效响应；实际文件只能经新端口完成。测试结束关闭放弃的旧 listener。
- `test_reconnected_handshake_rejects_reply_from_old_control_socket[upload/download]`：断开真实连接并确认旧 request 退役，同一 Client 实例重新连接；把新 request 的响应经旧 socket 回调入口交付，观察新 waiter 的结果仍为空，再经真实新控制连接交付并完成 32,781 字节文件。直接覆盖地址复用不能代替 socket 身份的边界。

### 4.3 独立真实加密程序

临时 `/tmp/pyflow-atomic5-smoke.py` 由 `.venv/bin/python` 执行，而非调用 pytest 测试函数。实际启动一个加密 Server 和两个加密 Client，隔离 key/progress/log 路径；全部断言通过，结束后全部被跟踪 worker 均退出。脚本在验证完成后移除。

| 路径 | 实际观测 |
|---|---|
| 公钥/控制加密初始化 | 两个 Client 均进入 framed 通道 |
| 普通目录项双向传输 | 每方向 131,089 字节，`ordinary/nested/item.bin` 原路径及完整字节相等 |
| 普通测试 payload SHA-256 | `cec551561b4ceedbc5845b735317c04c73bf1b20627a76b960a5602de26b7e4c` |
| 同源并发中继 | 98,315 字节目录项与 81,927 字节普通文件；第一上传 port 阻塞期间第二文件先完成 |
| 中继目的路径 | 第一项仅在 `kept/nested/first.bin` 出现，不落到根目录 |
| 定向失败隔离 | 另发不存在目标的上传返回 False，不使两个有效上传失败 |
| port/stamp 配对 | 两个独立 request 分别带 `enc:` stamp；接收 attach 均使用加密 stamp，最终文件字节正确 |
| 数据通道原始输入 | 捕获 target SecureChannel.feed 的输入，不含完整测试文件明文 payload |
| 成功收尾 | source upload、Server route/relay 及三方 pending request 均为空 |

这里的原始输入观测不是系统级 pcap、完整日志脱敏审计或所有安全路径的全局证明。

### 4.4 文档、翻译与静态检查

- `.venv/bin/python -m sphinx -b html docs/source /tmp/pyflow-atomic5-html`：27 个源文件构建成功，2 条警告：配置的 `_static` 不存在；`command_control_extension_tcp` docstring 第 23 行 definition list 缺空行。未改动这些非本次问题。
- `polib` 读入全部 135 个 `.po`：8,882 entries，parse_errors 为空。没有全局 `sphinx-intl` 再生成或翻译评审。
- 生产与活动文档范围检索未发现旧端口缓存、等待函数、FIFO 或共享错误槽消费者；`/forward_upload` 仅保留在拒绝旧响应的测试与说明其删除的文档中，不是兼容入口。
- 最终 `ruff check --select F,E9` 覆盖 `connect_tcp.py`、helper、握手单测及受影响 integration 文件，结果仍为 **1 个 F841**：`connect_tcp.py:6975` 的 custom-handler 分支把返回值赋给未使用的 `response`。原子 4 报告已记录同一分支（当时行号 6937）；本次未改动该非握手问题。测试中迁移后遗留的两处无用变量已移除。不得将本项描述为 lint 全绿。

## 5. 路径逐项结论

| 需求路径 | 结论与证据 |
|---|---|
| 双向普通文件/目录内文件 | 完整 socket 套件通过；独立加密程序双向目录项内容/路径通过；multiple_file/multiple_file_folder 回归保留并执行 |
| 双向公钥及并发 key exchange | 加密套件含 `test_concurrent_clients_share_key_exchange`，完整套件通过；独立程序两端 key exchange 完成 |
| 中继各目标 listener | 一对多转发、目标失败后恢复/route 及并发上传回归通过，每个 request 独立匹配 |
| 中继源上传乱序 | 定向真实 socket 回归与独立加密程序均观测第二文件先完成 |
| 超时/取消/错误 FID/重复/定向错误 | registry 行为单测；双向过期 listener 回归；独立失败隔离通过 |
| 控制断开、重连及迟到回调 | registry 单测及新增双向真实重连用例通过；旧 socket 不能填充新 waiter |
| nonce 与端口配对 | 共同结果对象；加密/公钥回归和乱序加密程序实际内容完成 |
| async、forwarding API 与恢复消费者 | 完整受影响套件通过；helper 和 required request 参数调用方已迁移 |
| 进度/route/ACK/稳定 FID | 不新增持久化 request；既有失败保留与稳定恢复断言保留并通过相关套件 |

## 6. 风险、未覆盖与人工闸门

- 新旧线路不兼容，必须同版本部署；未增加旧节点桥接或自动迁移。
- 更改 6 未获授权，未切换 FID 分配方，未删除 `tfid`/`relay_fid=`。不处理 client_port 身份迁移、中继落盘、暂停持久化或新重试协议。
- 本次没有 CI、依赖、coverage 阈值、加密算法或鉴权策略更改；保留既有断言，不以 mock 回声代替最终字节/路径/ACK 行为。
- 遗留 Ruff F841、两条文档警告及新文本翻译缺口如上公开；不声明已消除。
- 人工重点审查：实际 socket 捕获与断开清理、登记早于发送、消费/超时/异常退役、port/stamp 不拆分、各目标 request 独立、源上传错误不能唤醒另一请求、旧端口 cache 与命令无生产消费者。新增真实乱序/过期/重连回归和活动变更说明保留这些约束。
- 本报告仅交付更改 5 的审查材料，不代表人类已审计通过，不授权继续更改 6；未创建提交、未合并、未推送。

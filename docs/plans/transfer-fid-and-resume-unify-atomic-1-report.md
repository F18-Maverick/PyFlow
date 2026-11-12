# 原子更改 1 报告：传输状态按接收端隔离

状态：实现与局部验收完成；待人类审查。不进入原子更改 2。未创建 git 提交、未合并、未推送。

## 提交意图

为现有传输进度、活动 socket、暂停与端口匹配补齐接收端作用域，防止不同接收端或收发方向的同数值 fid 相互覆盖和清理。复用既有五项进度值，不新增 client 恢复专用表。

## 改动内容与数据流

- `PyFlow/network_api/connect_tcp.py`：接收进度仍以本地整数 fid 为键；发送进度、出站活动和暂停使用 `(receiver, fid)`。普通发送、恢复、公钥推送和中继目标端口收集按控制 peer 与 fid 同时匹配端口。接收监听不再清除同号出站端口通知。分配器在已知接收端作用域中检查占用，保持现有生成布局与分配方。
- Client 出站命名空间是配置的 server；转发上传进度值中的第五项仍是逻辑目标。Server 出站命名空间是接收 client 的控制端点。
- Server 本地 `pause_trans(fid, peer)` / `start_trans(fid, peer)` 要求显式接收端；Client 可省略 peer 使用其 server，拒绝其他显式 peer。远程暂停线路不变。
- 持久化 `upload` 键是紧凑 JSON 字符串 `[receiver, fid]`，`download` 仍为十进制字符串；五项值不变。遇到历史裸上传 fid 键，加载拒绝并记录原因；写回与关闭保留旧文件，不自动迁移或删除。归档旧文件后才能持久化新任务。
- 两个 Web 后端按收发方向提取数值 fid，保留既有 `/api/transfers` 字段。前端 JS、模板不变。
- 迁移文件/恢复/加密测试中的索引调用方；新增同号并发、跨方向清理、持久化、旧快照保护、Web 碰撞行和分配器占用行为回归。
- TCP 两端 API、文件传输文档、模块相关 docstring、批准计划与 `docs/source/changes/transfer-receiver-scope.md` 同步更新。

入口 → 出口：端口响应从已有控制连接获得 receiver → 缓存与等待同时匹配 receiver/fid → 发送腿登记 scoped key → 暂停和断线清理仅定位该 key → 完成按原 entry/socket 所有权释放 → 同样的 scoped key 写入及读回进度文件 → Web 仅输出数值 fid 与五项进度内容。

## 改前复现

使用修改前捕获的函数定义执行内存/socketpair探针，观察到：

1. 两个接收端的同号上传只剩 1 条进度，预期 2 条。
2. 清理入站 peer X，误关闭原本属于出站 peer Y 的同号 socket。
3. 两条同号端口响应中，等待 X 取走 Y 的端口。

对应永久回归保留在 `test_tcp_transfer_resume.py` 的同号真实 socket、跨方向 socketpair 和持久化场景中。

## 局部验证

### 相关传输与 Web 套件

```bash
.venv/bin/python -m pytest test/integration/network_api/test_tcp_transfer_resume.py test/integration/network_api/test_tcp_file_transfer.py test/unit/transfer_web/test_web_transfers.py test/integration/test_forward_extension.py test/integration/test_forward_msg_attribution.py test/integration/network_api/test_crypto_tcp.py test/unit/network_api/test_resume_matching.py -q
```

结果：108 passed，170.82s。包含真实文件/目录传输、转发、流控、普通/加密恢复、Web API 等现有及新增场景。

```bash
.venv/bin/python -m pytest test/unit/network_api test/integration/network_api/test_tcp_async_server.py -q
```

结果：109 passed，16.20s。

随后新增分配器作用域回归：

```bash
.venv/bin/python -m pytest test/unit/network_api/test_fid_allocator.py -q
```

结果：2 passed，0.02s。另运行最终版本的旧快照保护用例，1 passed，0.01s；覆盖两端、加载优先/写入优先、空状态/新 scoped 状态写回及关闭的字节不变性。上述命令间有用例交集，不能直接相加为唯一用例总数。

### 独立真实运行证据

用一次性脚本启动实际 `TCP_Server_Base` 与两个 `TCP_Client_Base`，未 mock 发送/接收关键路径，未通过 pytest 驱动：

- 两个接收端控制地址分别为 `127.0.0.1:54384`、`127.0.0.1:54394`；发送同一个数值 fid `708`，以相反接收端顺序领取端口 `37797`、`46447`。
- 上传快照同时保留 `[["127.0.0.1",54384],708]` 和 `[["127.0.0.1",54394],708]` 两个 JSON 键；两条任务暂停时进度均为 0。
- 将实际在途 server 交给真实 Flask `/api/transfers` 路由，登录后 HTTP 200；收到两行 fid 均为 708，文件名分别为 `source-0.bin`、`source-1.bin`，总大小 98,323 / 196,627 字节，未合并同号行。
- 清理第一个 receiver：其 socket 关闭；第二条腿的暂停保持 True、socket 仍开启。释放第二条腿后，文件长度 196,627 字节，源/目标内容完全相等，scoped 发送状态清理完成。
- 最终文件 SHA-256：`912e79f992326790f02f5b470f7fc473a7dc82fd366ec0ce5a1fd9d0005ee028`。
- 最终脚本退出码 0。一次性脚本和自建临时目录已移除；永久可重跑场景为 `test_equal_server_upload_fids_are_isolated_by_receiver`。
- 初次烟测脚本错误使用控制端口 0 导致超时；改为已分配端口后完成路径。随后脚本进程内清理目录与后台收尾写入发生竞争；改为进程退出后由父进程清理，最终退出码 0。两项均为烟测脚本修正，未为此改动协议实现。

### 静态检查

```bash
ruff check --select F,E9 PyFlow/network_api/connect_tcp.py PyFlow/transfer_web/web_backend/server_backend.py PyFlow/transfer_web/web_front/client_backend.py test/integration/network_api/test_tcp_file_transfer.py test/integration/network_api/test_tcp_transfer_resume.py test/integration/network_api/test_crypto_tcp.py test/unit/transfer_web/test_web_transfers.py test/unit/network_api/test_fid_allocator.py
```

退出码 1：仅 `connect_tcp.py` 既有 `response` 未使用（F841）。用本轮修改前捕获的源码运行同样规则，得到同一条诊断，证实为基线问题；未修改无关控制命令代码。未声称全量 lint 通过。

## 路径与验收结论

| 原子 1 路径 | 证据与结论 |
|---|---|
| 不同接收端同数值 fid | 真实两 client 并发传输与独立脚本通过；发送进度独立 |
| 跨方向同号清理 | 两端 socketpair 回归通过；收到的 EOF 仅属于被清理方向 |
| 定向暂停/继续 | 两 client 真 socket 回归与脚本通过；清理/释放一条不解除另一条暂停 |
| 普通/恢复端口匹配 | 按接收端匹配；现有普通双向与恢复套件通过 |
| 公钥及加密子连接 | `test_crypto_tcp.py` 通过；端口缓存测试验证控制 receiver |
| 转发目标端口和流控 | 既有转发文件、目录、多目标、流控及归属回归通过；不宣称已完成新版转发路由 |
| 持久化 | 同号两项读回且不覆盖 live 项；旧 upload 裸键快照按字节保留 |
| Web 消费者 | 两后端 Flask 回归通过；独立实际在途 server 的 Flask API 返回两条同号记录 |

## 明确未覆盖与风险

- 原子更改 2–6 未开始。当前 tfid/kind、发送端分配、`/forward_resume`、转发恢复专用记录、原有最终目标完成语义均尚未切换，不报告整个需求完成。
- 本次没有保证不同发送者入站同号都可共存；接收表仍按本端 fid 定位，最终由接收端分配的约束属于更改 6。
- 没有解决 client_port 改变、旧版本断点迁移、暂停持久化、中继落盘或关闭进度后自动续传。
- Web 只验证真实 Flask API 与数据适配，没有浏览器视觉验收；前端模板/JS 未改。
- 未运行整个仓库测试、完整 lint、文档构建或 Windows/macOS 验证。
- API 影响：发送进度字典的键、上传 JSON 索引，以及 server 本地暂停调用需要迁移。已有文件字节格式和线路命令不变。
- 核心文件相对本轮修改前快照为 +170/-148 行（净 +22）；新增/迁移测试与文档另计，整体超出原表的粗略 250–400 行估算。仍是同一个 receiver-scoping 意图；未创建 git 提交，人类可据此审查规模，未以行数为理由省略路径。

## 红旗与规则沉淀

未删除/跳过测试、弱化断言、修改 CI/依赖/coverage 或添加发送旁路。没有生成第二份进度记录模型或兼容别名。永久行为回归与变更说明固定“出站数值 fid 不是完整身份”的约束。

请人类审查入口到出口、公开键契约和上述未覆盖项；放行之前不进入原子更改 2。

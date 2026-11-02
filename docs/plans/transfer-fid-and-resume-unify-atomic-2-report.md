# 原子更改 2 报告：Server 单份 FID 路由

状态：实现与局部验收完成，等待人类审查。未创建 git 提交、未合并、未推送；未进入原子更改 3。

## 提交意图

将 server 的多 ID 转发恢复记录收敛为一份权威路由与接收端作用域索引，保证目标协商失败后仍保留完整接受目标集合，并可从持久化数据重建路由。

## 改动内容与数据流

- `PyFlow/network_api/connect_tcp.py`：server 删除 `_forward_resume_records`，替换为 `_fid_routes[upstream_fid]` 与 `_fid_route_index[(receiver, downstream_fid)] -> upstream_fid`。前者唯一保存 `originator` 与完整 `{receiver: fid}` 目标映射，后者只保存索引，不复制路径、文件名、目录、速度或字节进度。
- `_register_fid_route` 在主键或下游索引冲突时整体拒绝，不部分覆盖；`_lookup_fid_route` 区分本地上游与具体 receiver 的下游；`_drop_fid_route` 按对象所有权移除主记录和其索引，旧对象不能删除新登记状态。
- 复用 `_forward_relays_lock`。`_forward_relays` 保持为在途条件变量与流控状态，不成为第二份持久化路由。删除路由后释放锁，再更新已有进度文件，避免与加载的进度锁/路由锁反向嵌套。
- Server 的恢复分派、进度读回占用判断和分配器 live-table 引用迁移至新路由。收到目标恢复请求时，以实际控制 peer 和下游 fid 找到上游 fid，再发送原有 `/forward_resume`；未知 peer/fid 不回退至其他目标。
- `relays` JSON 每个上游 fid 只存一行：`originator` 和 `targets: [[receiver, fid], ...]`。加载重建派生索引，恢复计数以权威路由为单位，不覆盖 live 主键或下游索引。
- 旧 server relay 格式拒绝读取，并在写回/关闭时保留原文件，不建立兼容别名或新状态文件；client 的旧转发恢复记录及其 JSON 格式保持不变。
- 中继前去重目标地址。可达性初筛后传入中继的目标构成接受集合；端口、连接、加密附着或开始握手失败不再从集合中消失。整组清理条件从 `delivered >= len(target_conns)` 改为 `delivered == len(targets)`。

数据流：接受目标集合 → 登记唯一上游路由与 scoped 下游索引 → 各目标协商、数据泵送 → 全部接受目标成功才清理整组；未全部成功则保留映射 → 断线持久化每组一行 → 新实例重建同一组索引 → 具体接收端的恢复请求转换为原始上游 fid。

## 文件清单

修改：

- `PyFlow/network_api/connect_tcp.py`
- `test/integration/network_api/test_tcp_transfer_resume.py`
- `test/unit/network_api/test_fid_allocator.py`
- `docs/source/File_Transfer/File_Transfer.rst`
- `docs/source/Network_APIs/TCP_Server_APIs.rst`
- `docs/plans/transfer-fid-and-resume-unify-plan.md`

新增：

- `test/unit/network_api/test_fid_routes.py`
- `docs/source/changes/transfer-fid-routes.md`
- 本报告。

未修改 client 公共接口、Web 适配、CI、依赖、coverage 或文件字节格式。核心文件相对本轮开始捕获的已提交源码为 +95/-77 行（净 +18）；新增集成场景与单测使整体审查量高于原 200–350 行粗估，仍保持单一 server 路由意图，没有省略验收路径。

## 改前复现与改后独立烟测

使用一次性脚本启动真实 server、源 client、健康目标和故障目标；故障仅注入在目标监听协商边界，发送真实控制响应宣布拒绝连接的端口，不 mock 数据泵或成功结果。

改前：健康目标完整接收 131,109 字节，故障目标未建立数据连接，源端返回上传成功；server 的 `relays` 计数为 0。脚本因“接受目标协商失败却删除整组恢复路由”断言失败，退出码 1。

改后同一场景：

- 健康目标完整接收 131,109 字节，目标文件与源文件完全一致。
- 接受集合包括 `127.0.0.1:48548` 与拒绝监听的 `127.0.0.1:48560`；原始源端为 `127.0.0.1:48546`。
- 上游 fid `6547520319`；两个下游 fid 分别为 `10842487615`、`15137454911`。
- 保留 1 份权威路由、2 个下游索引；新实例中所有查找指向同一权威对象，未知 receiver 被拒绝。
- 健康目标文件 SHA-256：`507205331a573dd31fb8f7959182403edb5232f7915297c7c8baec98d3e6eefd`。
- 原传输进程退出码 0。随后另起独立 Python 进程，只从 `server-progress.json` 加载，恢复计数 1、接受目标数 2、索引数 2；共享权威对象与错误 receiver 拒绝检查均通过，退出码 0。
- 独立进程读回验证的是持久化路由及索引重建，不是声称跨服务重启的自动文件续传已端到端完成。
- 一次性烟测/读回脚本与自建临时目录已清理；永久真实 socket 场景保留在集成测试中。

重要边界：烟测中源端仍可能返回上传成功，这是现有最终 ACK 语义，属于原子更改 3。本次仅保证 server 不丢路由，不能据此声称源端记录生命周期已修复。

## 测试与检查

```bash
.venv/bin/python -m pytest test/unit/network_api test/integration/network_api/test_tcp_transfer_resume.py test/integration/network_api/test_tcp_file_transfer.py test/integration/network_api/test_crypto_tcp.py test/integration/test_forward_extension.py test/integration/test_forward_msg_attribution.py test/integration/network_api/test_tcp_async_server.py -q
```

结果：208 passed，179.72s。覆盖现有普通/转发文件、目录、加密、恢复与异步路径，以及新增路由和 socket 场景。套件输出包含一条 `Server error: [Errno 9] Bad file descriptor`，未造成测试失败；本次未将该输出当作已修复事项。

最终分配器断言加强后，单独执行：

```bash
.venv/bin/python -m pytest test/unit/network_api/test_fid_routes.py test/unit/network_api/test_fid_allocator.py -q
```

结果：11 passed，0.03s。与上一命令有交集，不相加为唯一用例数。

新增永久行为验证：

- 上游及多个下游查到同一权威路由。
- 不同 receiver 的同数值下游 fid，包含跨路由场景，互不混淆。
- 错误 peer/fid 拒绝，不部分覆盖主键/索引。
- 非 owning 对象不能清理现有路由。
- 单份 JSON 路由经新实例读回，索引重建且不覆盖 live owner。
- 旧 server relay 快照在写回/关闭后按字节保留。
- 分配器首个返回值同时避开已有上游与相应 receiver 的下游 fid。
- 真 socket：一个目标拒绝连接、另一个完成后仍保留目标全集；未知恢复请求不转发，正确请求在源端离线时进入原有待发队列。
- 真 socket：一个目标完成、另一个 writer 暂停时，路由与索引仍存在；释放后两份 payload 一致，路由与索引全部清理。

静态命令：

```bash
ruff check --select F,E9 PyFlow/network_api/connect_tcp.py test/integration/network_api/test_tcp_transfer_resume.py test/unit/network_api/test_fid_routes.py test/unit/network_api/test_fid_allocator.py
```

退出码 1，仅既有 `response` 未使用（F841）。对本轮修改前捕获的已提交源码运行同样规则，得到同一诊断；不声称全量 lint 通过，也未修改无关控制命令代码。

## 明确未覆盖与风险

- 原子更改 3–6 未实施：源端最终完成通知与记录保留、逐目标完成状态、统一 resume、client 专用恢复表删除、握手合并、kind/tfid 删除、接收端分配权切换均仍待批准。
- 当前若部分目标成功，路由保留整个接受映射；尚不单独清理成功目标或解决其重复恢复问题。
- 当前 `/forward_resume`、`/forward_upload`、kind 与 tfid 继续生效。它们在更改 2 后存在不是遗漏。
- 不改变可达性初筛的既有跳过行为：提交转发请求时已不可达或是 server 自身的目标仍被跳过；本次保证的是被接受后协商失败的目标不误算成功。
- 不迁移旧 server relay 快照、不解决 client_port 改变、中继内容落盘或暂停持久化。旧快照需人工归档后再持久化新路由。
- 无全仓测试、完整 lint、文档构建或跨平台验证；本次未改 UI。

## 审查与规则沉淀

未删除或跳过测试、弱化断言、改 CI/依赖/coverage，未增加直接发送旁路；server 旧表已切除，不保留兼容别名。测试和变更说明固定两条约束：下游身份包含 receiver；接受目标全集不能被连接成功子集替代。

请人类审查上述入口到出口、单份状态及未覆盖边界。未获下一项明确放行前，不进入原子更改 3。

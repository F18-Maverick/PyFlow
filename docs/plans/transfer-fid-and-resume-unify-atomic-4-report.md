# 原子更改 4 报告：统一恢复入口与 Client 状态收敛

状态：已实施，待人类审查；未创建 git 提交、未合并、未推送；未进入原子更改 5/6。

## 1. 变更范围与数据流

本次只覆盖计划第 7 节。入口是普通重连、转发目标重连、目录项 EOF 再确认、离线源端待发和重复恢复请求；出口是原有 `/file`/`/forward_item` 数据连接、既有最终 ACK、稳定 FID 的进度清理或失败保留。

统一线路：

```text
receiver -> /resume_trans <fid> <offset> <origin_repr> <target_repr> -> connected peer
```

两个地址是 shell-quoted 二元组。`parse_resume_command` 只接受五个 token，返回 `(fid, offset, origin, target)`；负 fid/offset、非 tuple 地址、非字符串 host、非整数 port、旧三 token 和 `/forward_resume` 均拒绝。`format_resume_line` 是唯一构造入口。

Server 收到请求后先要求 `target ==` 实际控制连接地址；`origin == (server.host, server.port)` 走本地 scoped upload，否则必须同时匹配 `(requester, downstream_fid)` 路由索引和 route originator，再把请求转成稳定 upstream fid 给源端。未知、伪造来源、伪造目标和同数值跨 receiver FID 不降级到另一条路径。源端离线时，Server 以 `(originator, upstream_fid, target)` 合并待发项，只保留最新 offset；安全发送成功后删除，发送失败保留。

Client 只在 `origin == client_socket.getsockname()` 时处理 Server 推送的恢复请求。直接目标使用配置 Server 的 scoped upload 行；转发目标也只从该五项 upload 行取源路径，通过既有 `relay_fid=`/`/forward_item` 重放稳定 upstream FID。目标端保留原接收行和完整嵌套路径，因此源端不复制 targets、destination、folder metadata 或第二张恢复表。

同一 scoped upload 的 transient `_resume_sends` 只含 `lock`、`users`、`targets`：同目标重复加入，异目标串行，结束释放；不写入 JSON。普通恢复和转发恢复共用它。`Client._transfer_progress_snapshot()["relays"]` 固定为空；非空旧 Client relay 区域拒绝读取并保留原文件。Server `_fid_routes`/`relays` 权威路线不变。

FID 不再编码 plain/relay kind：删除 kind 常量、解析函数和 allocator kind 参数；保留低位为零，日志格式为 `e<epoch>.<counter>`。新传输的既有分配方、`tfid` 命名、`/forward_upload` 和 `relay_fid=` 暂不改变，属于后续 5/6。

## 2. 修改文件

- `PyFlow/network_api/connect_tcp.py`
  - 五 token resume parser/formatter 与所有发送、分派、Server route 翻译调用方；
  - 删除 `/forward_resume`/kind/Client duplicate recovery state；
  - Client upload-row-only recovery；Server offline pending queue；transient send serialization；
  - module/class/docstring state contracts and legacy snapshot preservation.
- `test/unit/network_api/test_resume_matching.py`
  - five-token format/parse round trip and malformed/old-wire rejection.
- `test/unit/network_api/test_fid_routes.py`
  - endpoint forgery, scoped downstream lookup, offline latest-offset coalescing, failed-send retention and route pruning.
- `test/unit/network_api/test_transfer_progress_query.py`
  - nonempty legacy Client relay snapshot rejection and byte-preserving write/shutdown behavior.
- `test/unit/network_api/test_delete_config.py`
  - transient `_resume_sends` deletion guard (existing supplemental regression migrated).
- `test/integration/network_api/test_tcp_transfer_resume.py`
  - unified direct/forwarded resume consumers; route identity; duplicate recovery; distinct-offset serialization; directory/full-path and EOF recovery.
- `test/integration/network_api/test_crypto_tcp.py`
  - Client relay snapshot assertion migrated to empty relays.
- Active documentation:
  - `docs/source/File_Transfer/File_Transfer.rst`
  - `docs/source/Network_APIs/TCP_Server_APIs.rst`
  - `docs/source/Network_APIs/TCP_Client_APIs.rst`
  - `docs/source/changes/transfer-fid-routes.md`
  - `docs/source/changes/transfer-receiver-scope.md`
  - `docs/source/changes/transfer-resume-simplified.md`
  - `docs/plans/ordinary-transfer-progress-inspection-plan.md`
  - `docs/plans/transfer-fid-and-resume-unify-plan.md`
  - `docs/plans/transfer-fid-and-resume-unify-requirements.md` marked superseded.
- Locale extraction catalogs: `docs/source/locale/{ja,ko,ru,zh_CN,zh_TW}/LC_MESSAGES/` entries for `File_Transfer/File_Transfer.po`, `Network_APIs/TCP_Client_APIs.po`, `Network_APIs/TCP_Server_APIs.po` and `api/PyFlow.network_api.connect_tcp.po` (20 catalogs).

The catalogs were not fully regenerated with `sphinx-intl`. To stop removed APIs from remaining in localized output, 80 obsolete extracted-message blocks were removed from those 20 catalogs. `polib` parsed all 135 catalogs successfully (8,936 entries total); a workspace search found no `forward_resume`, `resume_transfers` or `parse_forward_resume_command` entries. This is cleanup, not translation of the updated descriptions; catalog regeneration and review remain out of scope.

## 3. Verification evidence

### Parser and static syntax

```text
.venv/bin/python -m py_compile PyFlow/network_api/connect_tcp.py \
  test/integration/network_api/test_tcp_transfer_resume.py
```

Result: exit code 0.

Direct parser smoke:

```text
format_resume_line(7, 123, ("sender host", 1), ("target host", 2))
/resume_trans 7 123 '("sender host", 1)' '("target host", 2)'
parse_resume_command(line)
(7, 123, ('sender host', 1), ('target host', 2))
```

### Focused unit regression

```text
.venv/bin/python -m pytest \
  test/unit/network_api/test_resume_matching.py \
  test/unit/network_api/test_fid_routes.py \
  test/unit/network_api/test_transfer_progress_query.py \
  test/unit/network_api/test_delete_config.py -q
```

Result: **120 passed in 0.35s**.

### Focused live socket recovery regression

```text
.venv/bin/python -m pytest test/integration/network_api/test_tcp_transfer_resume.py \
  -k 'unified_resume or forwarded_file_resumes or forward_route_survives_a_refused_target_listener or download_resumes_over_the_encrypted_channel' -q
```

Result: **4 passed, 28 deselected in 14.53s**.

### Real plaintext and encrypted path probe

Throwaway `/tmp/pyflow-atomic4-smoke.py` started actual Server plus three Clients and exercised:

| Path | Evidence |
|---|---|
| ordinary download/upload | partial offset `12345`; original path and SHA-256 matched |
| forwarded two-target replay | stable upstream `fid=500`; offsets `32123`/`76543`; duplicate target request coalesced; both payload hashes matched |
| offline origin | pending `(fid,target)` retained; latest offset `54321` replaced `4096`; reconnect delivered and cleaned route/upload |
| encrypted equivalents | same three paths over framed channel; all payload hashes matched |

Probe output: **262,166 bytes**, same SHA-256 `2c189692b3fdcac0988b6563883b004a1165cd66f518b414c4c898e208ead97e` for all four paths in both plaintext and encrypted runs.

An additional throwaway live-socket smoke (script removed after execution) used an encrypted Server and Client, a 131,089-byte folder item at `original/nested/folder-item.bin`, and a rejected final ACK after EOF. The receiver then sent the five-token `/resume_trans` at offset 131,089; the original FID resumed over an encrypted data connection, the exact bytes and nested destination were preserved, and both progress rows cleared. Result: passed; worker teardown completed cleanly.

### Full affected suite

The initial full run reached **338 passed, 1 failed** because the new duplicate-target assertion assumed callback ordering not promised by Server dispatch. The assertion now waits for the observable target reservation and active transfer. The focused rerun passed; the fresh full run after that correction and the pending-state helper rename passed: **339 passed in 299.79s**.

### Documentation and static checks

- `.venv/bin/python -m sphinx -b html docs/source /tmp/pyflow-atomic4-html-final`: succeeded for 27 sources with 2 warnings: configured `_static` path is absent, and `command_control_extension_tcp` has a docstring definition-list formatting warning at line 23.
- `polib` parsed all 135 `.po` catalogs (8,936 entries); no parse errors. Obsolete resume API terms are absent from the locale catalogs.
- `.venv/bin/python -m py_compile` on `connect_tcp.py` and the affected unit/integration files: exit code 0.
- `ruff check --select F,E9` reported `F841` at `connect_tcp.py:6937` (`response` assigned but unused in the custom-handler branch). That branch is outside the atomic-4 recovery changes and was left untouched.

## 4. Path conclusions

- Ordinary bidirectional recovery: existing real-socket integration tests and the plaintext/encrypted probe.
- Forwarded one-to-one/multi-target, partial completion and stable-FID replay: focused integration, the live plaintext/encrypted probe and the full affected suite.
- Directory/full nested path and EOF second-failure/success: parametrized real-socket folder ACK/recovery cases plus the executed encrypted folder EOF probe above.
- Offline origin/reconnect and latest-offset coalescing: live probe and route unit tests.
- Same-target duplicate/different-target serialization: focused concurrency regression and fresh full-suite run.
- Legacy Client relay snapshot preservation and O3/O4 state boundaries: focused unit regressions.
- Public-key, async and forwarding-extension consumers: unchanged in this cutover; included in the full affected suite, which passed.

## 5. Explicit risks and non-coverage

- Full `sphinx-intl` catalog regeneration and translation review were not performed. The obsolete strings named above were removed from 20 affected catalogs; current changed source descriptions are not fully translated.
- Ruff still reports the unrelated unused local at `connect_tcp.py:6937`; it was not changed as part of this recovery cutover.
- No client-port identity migration, handshake consolidation, receiver-owned allocation switch, dependency/CI/coverage change, automatic old-state migration, retry protocol, or relay disk persistence was added.
- No git commit, merge or push was performed.

## 6. Human review gate

This report stops at atomic change 4. Review the endpoint validation, route ownership, pending queue removal ordering, scoped upload lookup, transient lock lifecycle, old snapshot preservation, and the evidence limitations above. Atomic changes 5/6 remain gated and are not implemented.

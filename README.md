# 影视字幕本地化质检

一个仅使用 Python 标准库实现的字幕翻译、时间轴审核和交付服务。SQLite 保存项目、字幕版本、人员分配、时间点评论、术语表、复核意见和交付快照。

## 运行

```bash
python app.py --init
python app.py --port 8009
```

打开 <http://127.0.0.1:8009>。`--init` 会创建示例纪录片项目、`zh-CN` 草稿版本和一条术语规则。数据库默认是 `subtitle_qc.db`，可用 `--db` 或 `SUBTITLE_DB` 修改。

## 流程

1. 负责人创建项目、字幕版本和术语规则。
2. 为版本分配 `translator`、`timeline`、`reviewer`。
3. 翻译或时间轴成员保存字幕；每项包含 `expected_revision`，旧页面提交会返回 409。
4. 成员可对具体字幕或毫秒时间点添加评论。
5. 翻译/时间轴成员提交复核，复核人**逐句签署**：确认时把当时的文本和时间轴摘要（SHA-256 内容摘要）写入句级签署。
6. 复核阶段翻译仍可改句；保存与签署/批准共用 `BEGIN IMMEDIATE` 写锁，只有真正改动的句子会按摘要失效，并记录失效原因（文本/句号/时间轴），其它句子的签署保持有效。
7. 复核人整版批准时逐句核对当前摘要，任一句缺少有效签署即返回 409 并列出句号；退回则回到草稿，签署保留。
8. 负责人锁定已批准版本，再执行交付。交付再次逐句核对当前摘要，缺少有效签署时列出句号并停止，不生成任何交付记录。
9. 交付时生成确定性的 SHA-256 快照（每句带 `content_digest`/`text_digest`/`timeline_digest`）；同语言的新交付会把旧版本标记为 `superseded`，但旧快照不会删除或覆盖。

字幕保存会验证时长范围、起点小于终点、字幕重叠、序号冲突和术语表。术语表中配置的禁用译法会直接阻止保存；指定译法可用。

## 句级签署

- 签署与字幕 `(version_id,cue_id)` 一一对应；重新签署失效句是原地更新，不会产生第二份签署。
- 批量确认支持 `batch_key`：客户端中断后用同一 key 和相同 `cue_ids` 重发即可恢复，重复处理幂等（批次表对 `(version_id,batch_key)` 唯一）。
- `GET /api/versions/{id}/signatures` 返回每句签署状态、复核人、签署/失效时间、失效原因，以及 `matches_current`（当前内容是否与签署摘要一致）。数据、校验与页面分开维护，重开页面仍可看清状态。
- 旧数据升级：重新打开数据库时，处于已批准/锁定/已交付/已取代状态的旧版本会按**当前内容**补成句级签署；已经交付的版本优先按**交付快照**补齐，确保签署描述的正是当时出门的内容。升级幂等。

## API

所有身份通过 `X-User`、`X-Role` 请求头模拟，角色包括 `owner`、`admin`、`translator`、`reviewer`、`timeline`。

- `POST /api/projects`：创建项目和成片校验信息。
- `POST /api/projects/{id}/versions`：创建目标语言版本，可指定同语言父版本。
- `POST /api/projects/{id}/glossary`：设置指定译法和禁用词。
- `POST /api/versions/{id}/assignments`：分配角色。
- `POST /api/versions/{id}/cues`：新增或修改字幕，要求 `expected_revision`；复核阶段改句会使该句签署失效。
- `POST /api/versions/{id}/comments`：按具体时间毫秒或字幕 ID 评论。
- `POST /api/versions/{id}/signatures`：逐句签署，body 为 `{"cue_ids":[…]}` 或 `{"all":true}`，可带 `batch_key`；返回本次签署、已有效、剩余句号。
- `GET  /api/versions/{id}/signatures`：句级签署状态、失效原因与 `matches_current`。
- `POST /api/versions/{id}/submit|review|lock|deliver`：完成审核交付状态机；`review` 的 approve 与 `deliver` 都逐句核对当前摘要。
- `GET /api/versions/{id}/cues|comments`、`GET /api/deliveries`：查看结果。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖完整复核交付流程、句级签署与改句失效、批准/交付逐句门槛、批量确认幂等可恢复、并发保存与批准不变量、旧版本（当前内容与交付快照两种）升级、时间轴重叠、术语禁用和人员权限。

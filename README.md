# 影视字幕本地化质检

一个仅使用 Python 标准库实现的字幕翻译、时间轴审核和交付服务。SQLite 保存项目、字幕版本、人员分配、时间点评论、术语表、复核意见、**逐句签署**和交付快照。

## 运行

```bash
python app.py --init
python app.py --port 8009
```

打开 <http://127.0.0.1:8009>。`--init` 会创建示例纪录片项目、`zh-CN` 草稿版本和一条术语规则。数据库默认是 `subtitle_qc.db`，可用 `--db` 或 `SUBTITLE_DB` 修改。

## 代码分层

- `validation.py`：纯领域校验，不接触 SQL/HTTP——字幕“文本+时间轴摘要”（SHA-256）、字段校验、术语校验、失效原因判定。
- `subtitle_db.py`：数据层——表结构、旧库迁移、句级签署、复核状态机、逐句核对与确定性交付。
- `app.py`：HTTP 薄层，只做请求解析、身份头透传和静态资源分发。
- `static/index.html`、`static/styles.css`、`static/app.js`：页面三层分开维护。

## 句级签署规则

复核签署**绑定到每一句字幕**，而不是整版：

1. 复核人逐句（或批量）确认时，记录该句当时的句号、起止毫秒与文本的内容摘要（`cue_approvals.content_digest`）。
2. 翻译/时间轴之后修改某句，只让**该句**的有效签署失效，并记录失效原因（文本已修改 / 时间轴已修改 / 句号已调整）；未改动的句子不受影响。内容相同的重复保存不会使签署失效。
3. 保存、签署、整版批准、锁定、交付都在 `BEGIN IMMEDIATE` 事务里按当前摘要判定，串行执行。翻译保存与复核批准并发时必须排队，**不可能出现签署对应旧内容却仍可交付**。
4. 批量确认带 `batch_id`：异常中断后用同一个批次号原样重提，已处理的句子幂等跳过，不会生成两份签署（`UNIQUE(batch_id,cue_id)` 与有效签署部分唯一索引共同保证）。
5. 整版批准在同一事务内逐句核对当前摘要；**交付逐句核对当前摘要，任一句缺少有效签署就列出句号并停住，不生成交付记录**。
6. 退回修改后版本回到草稿，未受改动影响的句子保留有效签署；改回原文的句子必须重新签署，旧失效行保留作审计痕迹。
7. 交付快照（确定性 SHA-256）内嵌每句摘要与签署人，旧交付不会被覆盖。

### 旧数据升级

数据库用 `PRAGMA user_version=2` 标记。升级旧库时自动补签：

- 整版已批准（尚未交付）的版本：取每版最后一次 approve 记录，**按当前内容**补成句级签署（`source=legacy_review`）。
- 已交付的版本：**从交付快照**按每句当时的内容补签（`source=legacy_delivery`）；当前已改或已不存在的句子按快照独立留存（`cue_id=NULL`），历史签署不丢也不误挂。

## 流程

1. 负责人创建项目、字幕版本和术语规则。
2. 为版本分配 `translator`、`timeline`、`reviewer`。
3. 翻译或时间轴成员保存字幕（草稿或复核中都可改，改动句的签署随之失效）；每项包含 `expected_revision`，旧页面提交返回 409。
4. 成员可对具体字幕或毫秒时间点添加评论。
5. 翻译/时间轴成员提交复核；复核人逐句批量签署后整版批准或退回。
6. 负责人锁定已批准版本，再执行交付；交付前逐句核对摘要。
7. 同语言新交付会把旧版本标记为 `superseded`，旧快照不删除、不覆盖。

字幕保存会验证时长范围、起点小于终点、字幕重叠、序号冲突和术语表。禁用译法直接阻止保存。

## API

身份通过 `X-User`、`X-Role` 请求头模拟，角色包括 `owner`、`admin`、`translator`、`reviewer`、`timeline`。

- `POST /api/projects`：创建项目和成片校验信息。
- `POST /api/projects/{id}/versions`：创建目标语言版本，可指定同语言父版本。
- `POST /api/projects/{id}/glossary`：设置指定译法和禁用词。
- `POST /api/versions/{id}/assignments`：分配角色。
- `POST /api/versions/{id}/cues`：新增或修改字幕，要求 `expected_revision`；返回失效签署数与原因。
- `POST /api/versions/{id}/comments`：按具体时间毫秒或字幕 ID 评论。
- `POST /api/versions/{id}/cues/approve`：逐句批量签署。body 可带 `cue_indexes`（默认全部）与 `batch_id`（中断恢复时原样带回）；返回 `signed` / `already_signed` / `missing_indexes`。
- `POST /api/versions/{id}/submit|review|lock|deliver`：复核交付状态机；批准/锁定/交付缺签署时列出句号并返回 409。
- `GET /api/versions/{id}/cues|comments|approvals|readiness`、`GET /api/deliveries`：查看字幕、评论、**逐句签署状态（含失效原因）**、交付就绪度与交付记录。

## 测试

```bash
python -m unittest discover -s tests -v
```

覆盖：完整复核交付流程、只让被改动句失效、文本/时间轴失效原因、未改动保存不失效、批准与交付列句号拦截、改回原文后重签且有效行不重复、批次幂等与并发单签、保存/签署并发下不留陈旧有效签署、旧库两类补签迁移、分层文件约束。

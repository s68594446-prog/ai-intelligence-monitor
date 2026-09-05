# Phase 3C 日报运维手册

文档核对日期：2026-09-05。只适用于每日全球 AI 产业链摘要；不改变冻结
Phase 0–2 Harness、Evidence、State Machine、Signal、Adjudication、Replay、Ops。

## 执行位置与证据

日报由 GitHub-hosted runner 执行，不依赖个人电脑开机或联网。计划时间为
22:30 UTC，即次日北京时间/台北时间 06:30；GitHub 调度可能延迟。

| 证据 | 能证明什么 | 不能证明什么 |
| --- | --- | --- |
| backend-ci 通过 | 离线回归通过 | Gemini、SMTP 或收件箱可用 |
| Digest Ops 通过 | 本次分析与覆盖满足现有检查 | 邮件已发送 |
| 邮件 READY / dry_run | 本地发送 Gate 和配置通过 | SMTP 已连接 |
| SENT 与 SMTP 接受时间 | SMTP 服务接受了发送请求 | 已进入每个收件箱、未进垃圾箱 |
| 收件端邮件/退信记录 | 特定收件人的实际结果 | 未来任务保证成功 |

## 日常诊断顺序

在 Git 根目录运行只读命令；不要显示 Secrets、`.env` 或完整 SMTP 地址：

```bash
gh run list --repo s68594446-prog/ai-intelligence-monitor --workflow daily-digest.yml --limit 5
gh run view <RUN_ID> --repo s68594446-prog/ai-intelligence-monitor
git status
git diff --check
```

先核对失败步骤、运行提交、日期和已存在的发送台账，再读取脱敏产物：
`summary.json`（如存在）、`ops.json`、`analysis-failures.json`、
`provider-request-events.json`、部门 `ops.json`/`provider-usage.json`。
使用具体运行目录，禁止混用旧 run 的通过结果和新日报。

| 现象 | 检查/处理 |
| --- | --- |
| 没有触发记录 | 核对 workflow 是否启用、默认分支、调度延迟；不是先查个人 Wi-Fi |
| 429 每分钟额度 | 查看一次有上限冷却及总预算；不能靠同项目多 Key 获得独立额度 |
| 每模型每日额度 | 仅在显式配置并启用时切换一次低一级模型；邮件须披露真实模型链 |
| 超时/503 | 查看有限重试和请求事件；不可无限重跑消耗额度 |
| provider halted / 未处理候选 | 不得降低覆盖分母、Evidence 或 60 分门槛来让 Ops 通过 |
| Evidence/数字引用不匹配 | 丢弃该分析，保留失败原因；不能补写证据 |
| SMTP 明确失败 | 修正对应配置/服务问题，核对台账再决定是否重试 |
| PENDING/UNKNOWN | 先核查 SMTP、台账、退信和收件端；不盲目再次发送 |

当前 workflow 最多抓取 50 个候选，允许 65 次 Gemini HTTP 请求（包含重试及
显式模型切换），CLI `--max-provider-requests` 硬上限 80。所有实际待分析候选
仍计入覆盖率；请求预算不是新闻条数目标。共享运行时限为 2400 秒。
不要把免费层理解为无限免费，模型与项目配额以运营时实际配置为准。

## 受控生成和补发

完整生成验收会真实消耗发现/Gemini 额度，但默认不发送、不提交产物：

```bash
gh workflow run daily-digest.yml --repo s68594446-prog/ai-intelligence-monitor --ref main -f send_email=false
```

邮件专用 workflow 只读取已经提交且带本次通过审计的日报，不重新分析。
明确指定日期，先 Gate 检查；缺少该日期时失败，不回退昨天：

```bash
gh workflow run email-digest.yml --repo s68594446-prog/ai-intelligence-monitor --ref main -f day=2026-09-05 -f send=false
```

仅在用户明确批准、核对收件配置与已有发送记录后，将上面的 `send=false`
改为 `send=true`。不要修改正文、日期或台账来绕过 ALREADY_SENT。
未生成可用日报时，必须先完成分析和 Ops，不能仅为了补发提交残缺内容。

两个发送 workflow 使用同一个 concurrency group。正常日程先通过 Ops/Gate，
再发送，成功后提交日报、来源历史和台账。邮件专用路径读取预先提交的合格
日报，发送后提交台账；使用该恢复路径时还需在成功后同步原运行产物中的
来源历史，保持跨日去重完整。

发送失败时 workflow 尝试仅保存台账，不发布失败日报。相同日期的未解决
PENDING/UNKNOWN 会阻止重发，即使重新生成的正文不同。明确 FAILED 可在
排除问题后重试。进程被取消、runner 丢失、Git 推送失败仍可能导致台账未落库；
这种情况下没有 exactly-once 保证，必须先调查，不能宣称“不可能重复发送”。

## 已验收的历史样本（不是未来运行保证）

- [2026-09-05 云端生成](https://github.com/s68594446-prog/ai-intelligence-monitor/actions/runs/33946862031)：
  34 个抓取候选全部尝试，33 个结构化输出通过，35 次 HTTP 请求，最终 13 条，
  未处理数 0，Ops 通过。主模型日额度触发后切换到配置的 Flash Lite；
  不合格证据仍被拒绝，未降低门槛。
- [2026-09-05 用户授权补发](https://github.com/s68594446-prog/ai-intelligence-monitor/actions/runs/33955299719)：
  SMTP 于台北时间 16:27:41 接受请求，收件人数 3，SENT 台账已提交。
  这不是收件箱到达证明。

## 未覆盖的保证

部门候选预路由依赖来源域名，不是完整的实体识别；发现源覆盖有限。
当前部门 replay 校验部分 Contract 并复制冻结 JSON，不逐一重算所有内嵌
产物哈希，也不重新执行语义审计。此轮没有修改这些边界或冻结 Replay。

# AI Intelligence Monitor

面向 AI 产业链研究的证据追踪与摘要工具。项目包含来源核验、研究记录、离线验证，以及可选的定时摘要与邮件交付流程。研究输出需要结合原始证据判断；测试通过不代表投资表现或生产交付质量已经验证。

## 开始使用

安装、配置和运行入口见 [后端项目说明](ai-intelligence-monitor/README.md)。先运行下方离线测试；启用外部模型、定时任务或邮件前，单独配置所需密钥与收件人。

## Repository layout

This repository root is a thin wrapper. Almost everything lives one level down.

| Path | Contents |
| --- | --- |
| [`ai-intelligence-monitor/`](ai-intelligence-monitor/) | The project itself: backend, config, prompts, docs, fixtures. Start at its [README](ai-intelligence-monitor/README.md). |
| [`digests/`](digests/) | Published daily digests plus the append-only send ledger and source history. Written by CI, not by hand. |
| [`.github/workflows/`](.github/workflows/) | `backend-ci` (tests), `daily-digest` (scheduled digest + gated send), `email-digest` (manual delivery), `env-probe` (manual Gemini probe). |

`digests/` sits at the root, not inside the project directory, because the
digest writer resolves it as the project's parent. Moving either directory
breaks that path and the workflows that commit into it.

## Where to read next

- [`ai-intelligence-monitor/ARCHITECTURE.md`](ai-intelligence-monitor/ARCHITECTURE.md) — Phase 3C department boundaries, gates, and artifacts.
- [`ai-intelligence-monitor/docs/PHASE_3C_RUNBOOK.md`](ai-intelligence-monitor/docs/PHASE_3C_RUNBOOK.md) — operating the daily digest.
- `ai-intelligence-monitor/docs/{PRODUCT_CONTRACT,HARNESS_STANDARD,SOURCE_STANDARD,SIGNAL_STANDARD}.md` — frozen normative standards.

## Tests

```bash
cd ai-intelligence-monitor/backend
python3.13 -m venv .venv && source .venv/bin/activate
python -m pip install -e ".[dev]"
pytest -q
```

The backend pins `requires-python >=3.13,<3.14`; CI uses 3.13.

## Working copy location

Keep this checkout on local disk. A clone inside an iCloud-synced folder
(`~/Documents` or `~/Desktop` when Desktop & Documents syncing is on) lets
macOS evict file contents to the cloud. Evicted files keep their size but hold
no local data, and Git then fails on them — typically
`fatal: mmap failed: Operation canceled` during `fetch` or `gc`. Check a
suspect checkout with:

```bash
find . -type f -flags +dataless | wc -l
```

Anything above zero means the working copy is being evicted; move it off the
synced folder.

## 开源协作

本项目采用 [MIT License](LICENSE)。参与开发请见 [CONTRIBUTING.md](CONTRIBUTING.md)，安全问题请见 [SECURITY.md](SECURITY.md)。

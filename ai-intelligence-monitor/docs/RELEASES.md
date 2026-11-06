# 版本、依赖与离线回放

## 2026-11-06：0.3.0

第二批将同日发送保护、只读 Watchdog、确认错误登记和跨日事件记录接入现有
Phase 3C。第三批改善手机邮件布局、固定安装版本并补充审计文件完整性回放。
部署证据以对应提交的 GitHub Actions 为准；版本号本身不证明线上发送成功。

| 标识 | 含义 |
| --- | --- |
| `app/version.py` 的 `0.3.0` | 软件包/API 元数据的唯一版本来源 |
| `daily-global-ai-analysis-v9` | 既有基础 Prompt，未修改 |
| `phase3c-department-contract-v1` | 既有部门合同，未修改 |
| `final-review-v3` | 最终正文封印，含确认纠错库哈希 |
| `phase3c-replay-integrity-v1` | 新增离线完整性封印格式 |

之前包版本 0.1.0 与 API 版本 0.2.0 不一致；统一元数据不改变冻结核心行为。

复查补丁：两个发送工作流在取得共享并发锁后，显式检出最新 `main`。
默认检出触发时提交可能遗漏排队期间前一任务刚提交的 SENT/PENDING/UNKNOWN
账本；仅让任务串行执行不足以修复旧快照。离线 Git 历史回归覆盖两个入口和
四类状态；明确 FAILED 仍允许按既有规则重试。没有增加发送动作或改变质量门槛。
这是已复现的旧快照风险，不代表已经发生过重复邮件；runner 在 SMTP 接受后、
账本尚未持久化前退出的既有限制仍需人工核对。
检出行为参考 [actions/checkout 官方说明](https://github.com/actions/checkout/blob/v5/README.md)。

## 固定安装

Python 3.13。在新的虚拟环境中，从 `backend` 执行：

```bash
python -m pip install --require-hashes --only-binary=:all: -r requirements-dev.lock -r requirements-build.lock
python -m pip install --no-deps --no-build-isolation -e .
python -m pip check
python -m pytest -q
```

实际生成/邮件工作流使用 `requirements.lock` 替代 `requirements-dev.lock`。
所有第三方运行、测试与构建包固定版本和下载哈希；本项目由受审查的 Git 提交固定。
运行和测试版本基于第二批 CI 42192385496 的成功安装，不自动升级依赖。
构建输入 `requirements-build.in` 与 `pyproject.toml` 的 build-system 保持一致。

人工更新时使用 uv 的通用 Python 3.13 解析；`-c requirements-dev.lock` 可保留
已验证版本，明确升级时才调整约束。先更新开发锁，再据此生成运行锁，最后
验证 macOS 和 Linux，审核变更后提交。示例（从 backend 执行）：

```bash
uv pip compile pyproject.toml --extra dev --universal --python-version 3.13 --generate-hashes --no-header --no-annotate -o requirements-dev.lock
uv pip compile pyproject.toml -c requirements-dev.lock --universal --python-version 3.13 --generate-hashes --no-header --no-annotate -o requirements.lock
uv pip compile requirements-build.in --universal --python-version 3.13 --generate-hashes --no-header --no-annotate -o requirements-build.lock
```

锁文件不替代安全更新审查；它也不保证包仓库可用。runner 系统镜像、pip 启动
环境和工具本身未做完整环境镜像。参考 [uv 锁定说明](https://docs.astral.sh/uv/pip/compile/)
与 [pip 哈希安装](https://pip.pypa.io/en/stable/topics/secure-installs/)。

## Phase 3C 回放

```bash
python -m app.scripts.replay_digest_run --run-dir /absolute/path/to/run
python -m app.scripts.replay_digest_run --run-dir /absolute/path/to/run --output /absolute/path/to/new-replay
```

第一条只读；第二条先验证完整快照，再向新目录写入原始字节。零网络、零模型、
零 SMTP。覆盖六个部门全部 8 类文件、部门 manifest、来源束、发现/抓取/分析、
请求及失败事件、最终审核、跨日记录、发布历史，以及存在时的正文和 A 股产物。
只复制固定允许列表；不复制密钥、发送账本或任意附加文件。

校验文件 SHA-256、四类 handoff 引用哈希、部门 manifest、合同、部门和 run ID。
`integrity_passed` 与 `delivery_allowed` 分开记录：失败日报也可以完整回放，
不会因此变成可发布。此步骤不重新判定事实真伪，也不等价于业务 Ops 或入箱证明。
旧产物缺少封印时返回 `REPLAY_LEGACY_UNSEALED`，不自动补盖新封印。
校验和用于发现意外改动，不抵抗文件与封印被一起恶意重写。

## 回退与成本

两批使用独立提交/PR。先回退第三批，再在必要时回退第二批：
`git revert <batch-code-commit>`，通过后端回归和 CI 后推送。
保留已产生的日志、账本、日报及纠错证据，不用删除目录或重置用户工作区回退。
回退不授权重新发送已经发送的日报。渲染变化后，旧正文封印可能不再匹配新渲染器，
应由人工决定是否离线复核；不能自动绕过封印或同日发送门禁。

不新增模型调用或默认每日模型费用。Watchdog 消耗少量 Actions 时间与最多三次
GitHub 只读请求；哈希与回放只增加本地 CPU、少量磁盘和 CI 时间。

# oracle-grab

用 GitHub Actions 定时抢 Oracle Cloud 免费 ARM（Ampere A1）实例，不需要自己的电脑一直开机。

## 原理

Oracle 免费 A1 实例经常报 `Out of host capacity`，只能反复调 `LaunchInstance` 接口碰容量。
本仓库的 `grab.py` 每 60 秒试一次，抢到就停，并把结果写进 `result.txt`，工作流随后开一个 Issue 提醒你。

- 每 5 小时被定时拉起一次，单次最多跑 5 小时 42 分（GitHub 单任务上限 6 小时）
- `concurrency` 保证同一时间只有一个任务，不会重复开两台
- 账号里已经有一台 A1 时会自动跳过（见 `--allow-multiple`）

## 需要配置的 Secrets

在仓库 **Settings → Secrets and variables → Actions** 里添加：

| Secret | 说明 |
| --- | --- |
| `OCI_USER_OCID` | API 密钥配置里的 `user=` |
| `OCI_TENANCY_OCID` | `tenancy=` |
| `OCI_FINGERPRINT` | `fingerprint=` |
| `OCI_REGION` | 例如 `us-sanjose-1` |
| `OCI_API_KEY` | 下载的私钥 `.pem` **完整内容**（含 BEGIN/END 行） |
| `OCI_SSH_PUBKEY` | 你电脑上 `~/.ssh/xxx.pub` 或 `~/oracle-grab/id_ed25519.pub` 的内容 |
| `OCI_SUBNET_ID` | 可选，不填则自动挑第一个可用子网 |
| `OCI_AD` | 可选，不填则用第一个可用域 |

## 怎么跑

- 自动：每 5 小时一次，无需干预
- 手动：**Actions → oracle-grab → Run workflow**

抢到后 Issue 里会有公网 IP 和 SSH 命令，用对应私钥登录：

```bash
ssh -i ~/.ssh/你的私钥 ubuntu@<公网IP>
```

## 注意

- 仓库建议用 **public**：私有仓库免费额度只有 2000 分钟/月，而 24 小时跑要 4 万多分钟；
  public 仓库的 Actions 对标准机器不计量。密钥存在 Secrets 里，公开仓库也不会泄露
  （fork 的 PR 拿不到 secrets，也不会自动触发本工作流）。
- 抢到实例后建议做点负载，避免再被 Oracle 按「闲置」回收。
- 密钥泄漏风险自查：不要在这个仓库里 `echo` 任何 secret，也不要加 `pull_request_target` 触发。

# Codex Worker Dispatcher

[English](README.md)

`codex-worker-dispatcher` 是一个跨平台的本地 Codex CLI 任务控制器。它可以启动与
当前终端分离的 worker、持久化可观察状态、执行任务 TTL，并提供按任务取消和经过
身份校验的恢复能力。正常操作命令只向 stdout 输出一个 JSON 对象；argparse 的
`--help` 是供人阅读的文本。

## 前置条件

- Python 3.10+。
- 已单独安装 `codex` CLI，并且可以通过 `PATH` 调用。
- Codex CLI 已完成认证，且能在目标工作目录正常使用。本项目不安装 Codex，也不
  管理登录凭据。
- 推荐使用 `pipx`；普通 Python 虚拟环境也可以。

安装前先确认两个工具：

```console
python --version
codex --version
```

## 安装

推荐通过 `pipx` 直接从 GitHub 安装最新版：

```console
pipx install git+https://github.com/holdonyb/codex-worker-dispatcher.git
codex-worker --version
codex-worker skill install
```

Skill 默认安装到 `~/.agents/skills/dispatching-codex-workers`。如果 Agent 工具不会
自动发现新安装的 Skill，请重启或重新加载。

也可以使用虚拟环境：

```console
python -m venv .venv
./.venv/bin/python -m pip install git+https://github.com/holdonyb/codex-worker-dispatcher.git
./.venv/bin/codex-worker skill install
```

在 Windows 上，最后两条命令分别使用 `./.venv/Scripts/python` 和
`./.venv/Scripts/codex-worker`。

## 路由预览

启动 worker 前可以先预览解析后的意图、沙箱、模型和推理等级；预览不会创建任务：

```console
codex-worker route --prompt "检查解析器并报告可能的边界情况。" --workdir /path/to/repo --intent read
```

不传 `--model` 时不会覆盖模型，worker 默认继承调用者当前的 Codex 配置。

## 只读示例

启动一个有时限的只读任务，并保存 JSON 响应里的 `task_id`：

```console
codex-worker start --prompt "检查解析器并报告可能的边界情况。不要修改文件。" --workdir /path/to/repo --intent read --sandbox read-only --timeout-sec 600
codex-worker status TASK_ID
codex-worker wait TASK_ID --wait-timeout-sec 120
codex-worker result TASK_ID
```

worker 的 TTL（`--timeout-sec`）和控制器等待期限（`--wait-timeout-sec`）不是一回事；
等待超时不会停止 worker。

## 限定范围的写入示例

写任务必须同时提供明确的写意图、`workspace-write` 和至少一个位于工作目录内部的
允许路径：

```console
codex-worker start --prompt "更新解析器及其聚焦测试。" --workdir /path/to/repo --intent write --sandbox workspace-write --allowed-path /path/to/repo/src/parser --allowed-path /path/to/repo/tests --timeout-sec 600
codex-worker wait TASK_ID --wait-timeout-sec 120
codex-worker result TASK_ID
```

允许路径是一份可审计的任务授权约定，不会创建操作系统级的子目录沙箱。接受修改
前必须检查 worker 产生的 diff，并运行父项目的验证。严禁使用
`danger-full-access` worker。

## 生命周期与恢复

保存每个任务 ID，直到任务进入 `completed`、`failed`、`timed_out`、`cancelled`、
`reaped` 或 `orphaned`，然后收集结果：

```console
codex-worker list
codex-worker status TASK_ID
codex-worker wait TASK_ID --wait-timeout-sec 120
codex-worker result TASK_ID
codex-worker cancel TASK_ID --wait-timeout-sec 30
codex-worker reap TASK_ID
```

先用 `cancel` 请求协作式停止。只有特定任务卡住时才用 `reap`；它会核对进程启动
身份、任务目录、角色和所有权 nonce，再终止属于该任务的进程树或进程组。它不会
按可执行文件名或宽泛的进程匹配来回收 worker。

陈旧任务清理默认只是 dry run，只有传入 `--apply` 才实际执行：

```console
codex-worker reap-stale --older-than-sec 3600
codex-worker reap-stale --older-than-sec 3600 --apply
```

## 状态文件与隐私

任务状态默认位于 `$CODEX_HOME/worker-runs`；未设置 `CODEX_HOME` 时位于
`~/.codex/worker-runs`。任务目录可能包含提交的提示词、工作目录、允许路径、进程
元数据、事件输出、诊断信息和最终消息。请把它当作私密数据：不要提交到 Git、
直接附加到 issue，或在未检查和脱敏前对外分享。需要隔离时使用 `--state-root`。

调度器本身不会上传这些状态。单独安装的 Codex CLI 仍会按照用户自己的 Codex
配置和条款进行通信。

## 支持的平台

| 平台 | 进程处理 | CI 目标 |
|---|---|---|
| Windows 11 / Windows Server | 原生分离式进程树、身份核对，且 worker 不额外弹出命令行窗口 | `windows-latest` |
| macOS | 分离式 POSIX 进程组和有界身份复查 | `macos-latest` |
| Linux | 分离式进程组、`/proc` 身份核对，并在可用时使用 pidfd 发送信号 | `ubuntu-latest` |

支持 Python 3.10 到 3.14。上表是 Task 10 的 CI 目标；只有 GitHub Actions 矩阵
成功后，发布文档才会记录跨平台验证已通过。

## 公开发布来源

公开 `main` 分支来自本地已验证 tracked tree 的单提交的净化快照。首次推送前，
快照会在临时的干净仓库中再次完成公开审计、测试和构建；私有开发历史不会被推送
到公开仓库。

## 升级与卸载

先升级 CLI，再升级由它管理的 Skill 副本：

```console
pipx upgrade codex-worker-dispatcher
codex-worker skill install --upgrade
```

`skill install --upgrade` 替换现有目录时会保留带时间戳的备份；确认升级正常后再自行
清理备份。

先显式卸载由本项目管理的 Skill，再移除 CLI：

```console
codex-worker skill uninstall --yes
pipx uninstall codex-worker-dispatcher
```

如果目录中没有有效的本项目所有权标记，Skill 卸载器会拒绝删除。使用虚拟环境的
用户可以在卸载 Skill 后删除该环境。

## 限制

- 当前是面向本地、有边界 Codex CLI 工作的 alpha 版本，不是远程队列、托管服务或
  多用户调度器。
- Codex 本身、身份认证、可用模型、用量限制和网络访问由用户负责。
- 允许路径用于表达授权范围，并不是操作系统级的子目录沙箱。
- 父 Agent 或操作者必须保存任务 ID、闭合每个任务的生命周期、检查输出和 diff，
  并运行项目级验证。
- 无法证明进程所有权时，恢复操作会主动拒绝终止该进程。

## 许可证

项目代码和文档使用 [Apache-2.0](LICENSE) 许可证。Codex CLI 是独立产品，本仓库
不会重新分发它。

## 非官方项目

这是一个非官方社区项目，与 OpenAI 不存在隶属、认可或赞助关系。“OpenAI”和
“Codex”仅用于说明本项目所兼容的、由用户另行安装的工具。

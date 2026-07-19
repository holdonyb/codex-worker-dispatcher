# 公开分享说明

这一页是可以直接发给别人的短说明。

## 这个仓库是什么

`codex-worker-dispatcher` 是一个公开的 Codex Skill，加上一套用于本地有边界
Codex CLI worker 分发的 runtime。它适合做本地 Agent 委派，支持任务 ID、
生命周期控制、取消和恢复。

仓库地址：

- https://github.com/holdonyb/codex-worker-dispatcher

验证过的发布版本：

- https://github.com/holdonyb/codex-worker-dispatcher/releases/tag/v0.1.1

最新全绿 CI：

- https://github.com/holdonyb/codex-worker-dispatcher/actions/runs/29653928879

## 能不能公开

可以。这个 public repo 是按公开发布流程整理出来的：

- 仓库从适合公开的净化快照开始
- 私有开发历史没有被推到公开仓库
- 首次公开前又重新做了一次审计、构建和测试
- GitHub CI 已在 Windows、macOS、Linux，以及 Python 3.10 / 3.14 上通过

但它仍然是一个非官方社区项目，与 OpenAI 没有隶属或背书关系。

## 别人应该怎么安装

大多数人直接安装 runtime，再让它安装托管 Skill：

```console
pipx install git+https://github.com/holdonyb/codex-worker-dispatcher.git
codex-worker skill install
```

Skill 会安装到：

- `$CODEX_HOME/skills/dispatching-codex-workers`
- 如果没设置 `CODEX_HOME`，则为 `~/.codex/skills/dispatching-codex-workers`

如果别人只想看 Skill 源码，也可以直接把这个仓库 clone 到自己的 Codex skills
目录。

## 对外发哪个链接

公开分享时，直接发下面这些链接就够了：

- 仓库：https://github.com/holdonyb/codex-worker-dispatcher
- 发布页：https://github.com/holdonyb/codex-worker-dispatcher/releases/tag/v0.1.1
- 英文 README：https://github.com/holdonyb/codex-worker-dispatcher#readme
- 中文 README：https://github.com/holdonyb/codex-worker-dispatcher/blob/main/README.zh-CN.md

## 平台状态

- Windows：支持，且 worker 启动时不会额外弹出命令行窗口
- macOS：支持
- Linux：支持

## 适用范围和限制

这个项目适用于“有边界的本地 Codex CLI 工作”。它不是：

- 托管服务
- 远程任务队列
- 多用户调度器
- 操作系统级子目录沙箱

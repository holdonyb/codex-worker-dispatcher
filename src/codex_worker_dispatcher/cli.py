import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from codex_worker_dispatcher import __version__
from codex_worker_dispatcher.errors import WorkerError
from codex_worker_dispatcher.installer import install_skill, uninstall_skill
from codex_worker_dispatcher.lifecycle import (
    cancel_task,
    list_tasks,
    reap_stale_tasks,
    reap_task,
    result_task,
    route_task,
    start_task,
    status_task,
    wait_task,
)


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise WorkerError("invalid_arguments", message, {})


def _add_route_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workdir", required=True, type=Path)
    parser.add_argument(
        "--complexity",
        choices=("auto", "simple", "standard", "complex", "hard", "extreme"),
        default="auto",
    )
    parser.add_argument("--intent", choices=("auto", "read", "write"), default="auto")
    parser.add_argument(
        "--sandbox",
        choices=("auto", "read-only", "workspace-write"),
        default="auto",
    )
    parser.add_argument("--allowed-path", action="append", default=[])
    parser.add_argument("--model")
    parser.add_argument(
        "--reasoning",
        choices=("auto", "low", "medium", "high", "xhigh"),
        default="auto",
    )


def _add_state_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-root", type=Path)


def _parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(prog="codex-worker")
    parser.add_argument("--version", action="store_true")
    subcommands = parser.add_subparsers(dest="command", parser_class=_JsonArgumentParser)

    route = subcommands.add_parser("route")
    route.add_argument("--prompt", required=True)
    _add_route_arguments(route)
    _add_state_root(route)

    start = subcommands.add_parser("start")
    prompt_group = start.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("--prompt")
    prompt_group.add_argument("--prompt-file", type=Path)
    _add_route_arguments(start)
    _add_state_root(start)
    start.add_argument("--timeout-sec", type=float, default=600.0)
    start.add_argument("--task-id")
    start.add_argument("--skip-git-repo-check", action="store_true")
    start.add_argument("--engine", choices=("codex", "fake"), default="codex", help=argparse.SUPPRESS)
    start.add_argument("--fake-delay-sec", type=float, default=0.0, help=argparse.SUPPRESS)
    start.add_argument("--fake-exit-code", type=int, default=0, help=argparse.SUPPRESS)

    for name in ("status", "result", "reap"):
        action = subcommands.add_parser(name)
        action.add_argument("task_id")
        _add_state_root(action)

    wait = subcommands.add_parser("wait")
    wait.add_argument("task_id")
    _add_state_root(wait)
    wait.add_argument("--wait-timeout-sec", type=float, default=600.0)

    cancel = subcommands.add_parser("cancel")
    cancel.add_argument("task_id")
    _add_state_root(cancel)
    cancel.add_argument("--wait-timeout-sec", type=float, default=30.0)

    list_parser = subcommands.add_parser("list")
    _add_state_root(list_parser)

    stale = subcommands.add_parser("reap-stale")
    _add_state_root(stale)
    stale.add_argument("--older-than-sec", type=float, default=3600.0)
    stale.add_argument("--apply", action="store_true")

    skill = subcommands.add_parser("skill")
    skill_actions = skill.add_subparsers(
        dest="skill_command",
        required=True,
        parser_class=_JsonArgumentParser,
    )
    skill_install = skill_actions.add_parser("install")
    skill_install.add_argument("--target", type=Path)
    skill_install.add_argument("--upgrade", action="store_true")
    skill_uninstall = skill_actions.add_parser("uninstall")
    skill_uninstall.add_argument("--target", type=Path)
    skill_uninstall.add_argument("--yes", action="store_true")
    return parser


def _read_prompt_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise WorkerError(
            "invalid_arguments",
            "Could not read prompt file",
            {"prompt_file": str(path)},
        ) from error


def _execute(args: argparse.Namespace) -> dict[str, object]:
    if args.command == "route":
        return route_task(
            args.prompt,
            args.workdir,
            args.complexity,
            args.intent,
            args.sandbox,
            args.allowed_path,
            args.model,
            args.reasoning,
        )
    if args.command == "start":
        prompt = args.prompt if args.prompt is not None else _read_prompt_file(args.prompt_file)
        return start_task(
            prompt=prompt,
            workdir=args.workdir,
            state_root=args.state_root,
            complexity=args.complexity,
            intent=args.intent,
            sandbox=args.sandbox,
            allowed_paths=args.allowed_path,
            model=args.model,
            reasoning=args.reasoning,
            timeout_sec=args.timeout_sec,
            task_id=args.task_id,
            skip_git_repo_check=args.skip_git_repo_check,
            engine=args.engine,
            fake_delay_sec=args.fake_delay_sec,
            fake_exit_code=args.fake_exit_code,
        )
    if args.command == "status":
        return status_task(args.task_id, args.state_root)
    if args.command == "wait":
        return wait_task(args.task_id, args.state_root, args.wait_timeout_sec)
    if args.command == "result":
        return result_task(args.task_id, args.state_root)
    if args.command == "cancel":
        return cancel_task(args.task_id, args.state_root, args.wait_timeout_sec)
    if args.command == "reap":
        return reap_task(args.task_id, args.state_root)
    if args.command == "list":
        return list_tasks(args.state_root)
    if args.command == "reap-stale":
        return reap_stale_tasks(args.state_root, args.older_than_sec, args.apply)
    if args.command == "skill" and args.skill_command == "install":
        return install_skill(args.target, upgrade=args.upgrade)
    if args.command == "skill" and args.skill_command == "uninstall":
        if not args.yes:
            raise WorkerError(
                "invalid_arguments",
                "Skill uninstall requires --yes; the JSON CLI never prompts",
                {"flag": "--yes"},
            )
        return uninstall_skill(args.target)
    raise WorkerError("invalid_arguments", "A command is required", {})


def _write_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.version:
            _write_json({"version": __version__})
            return 0
        value = _execute(args)
        _write_json({"ok": True, **value})
        return 0
    except WorkerError as error:
        _write_json(error.to_dict())
        return 2
    except Exception as error:
        wrapped = WorkerError(
            "internal_error",
            "Unexpected dispatcher failure",
            {"error": str(error)},
        )
        _write_json(wrapped.to_dict())
        return 1

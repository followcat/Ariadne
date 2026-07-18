"""rich-based terminal UI helpers for the CLI host.

Kept separate from render.py so json_mode never touches rich: machine
output must stay parseable (cli-shell-agent §6.2).
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from rich.console import Console
from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.table import Table

# decorations go to stderr so stdout stays clean for piping
console = Console(stderr=True, highlight=False)
out = Console(highlight=False)


def print_assistant(text: str) -> None:
    """Final assistant text as markdown on stdout."""
    out.print(Markdown(text))


def print_tool_start(name: str, args: dict[str, Any]) -> None:
    console.print(f"• [bold cyan]{name}[/]", highlight=False)
    cmd = args.get("cmd") if isinstance(args, dict) else None
    if cmd:
        console.print(f"  [dim]$ {cmd}[/]")


def print_tool_done(name: str, status: str, output: Any = None) -> None:
    mark = "[green]•[/]" if status == "completed" else "[red]×[/]"
    console.print(f"{mark} [bold cyan]{name}[/] {status}")
    if isinstance(output, dict) and output.get("diff"):
        print_diff(str(output["diff"]))


def print_diff(diff: str) -> None:
    console.print(Syntax(diff, "diff", theme="ansi_dark", line_numbers=False))


def print_error(code: str, message: str) -> None:
    console.print(f"[bold red]ERROR {code}[/]: {message}")


def print_info(text: str) -> None:
    console.print(f"[dim]{text}[/]")


def print_usage(prompt: int, completion: int, total: int, reasoning: int = 0) -> None:
    console.print(
        f"[dim][usage prompt={prompt} completion={completion} total={total} reasoning={reasoning}][/]"
    )


def print_table(headers: list[str], rows: list[list[str]]) -> None:
    table = Table(show_lines=False)
    for h in headers:
        table.add_column(h)
    for row in rows:
        table.add_row(*row)
    out.print(table)


@contextmanager
def status(message: str) -> Iterator[None]:
    """Spinner while a turn/tool is in flight."""
    with console.status(f"[dim]{message}[/]", spinner="dots"):
        yield


def print_delta(text: str) -> None:
    out.print(text, end="", highlight=False)


def print_event_line(kind: str, detail: str) -> None:
    console.print(f"[dim]{kind}[/] {detail}")


def render_result(result: Any, *, verbose: bool = False, skip_text: bool = False) -> None:
    """Pretty-print a TurnResult: tool blocks, errors, assistant text, usage."""
    if verbose or result.tool_calls:
        for call in result.tool_calls:
            mark = "[green]•[/]" if call.status == "completed" else "[red]×[/]"
            console.print(f"{mark} [bold cyan]{call.name}[/]")
            cmd = call.arguments.get("cmd") if isinstance(call.arguments, dict) else None
            if cmd:
                console.print(f"  [dim]$ {cmd}[/]")
            if call.status == "failed" and call.error is not None:
                console.print(f"  [red]error {call.error.code}[/]: {call.error.message}")
                continue
            out_payload = call.output
            if isinstance(out_payload, dict):
                if out_payload.get("diff"):
                    print_diff(str(out_payload["diff"]))
                if out_payload.get("stdout"):
                    lines = str(out_payload["stdout"]).rstrip().splitlines()
                    for ln in lines[:40]:
                        console.print(f"  {ln}")
                    if len(lines) > 40:
                        console.print(f"  [dim]… ({len(lines) - 40} more stdout lines)[/]")
                if out_payload.get("stderr"):
                    console.print("  [yellow][stderr][/]")
                    for ln in str(out_payload["stderr"]).rstrip().splitlines()[:20]:
                        console.print(f"  {ln}")
                if "exit_code" in out_payload:
                    extra = [
                        flag
                        for flag in ("timed_out", "truncated", "compressed")
                        if out_payload.get(flag)
                    ]
                    suffix = f" ({', '.join(extra)})" if extra else ""
                    console.print(f"  [dim]exit {out_payload['exit_code']}{suffix}[/]")
            elif out_payload is not None:
                console.print(f"  {out_payload}")
    if result.status == "failed":
        err = result.error
        if err is not None:
            print_error(err.code, err.message)
        else:
            print_error("FAILED", "turn failed")
    elif not skip_text and result.text:
        print_assistant(result.text)
    if verbose and result.usage.total_tokens:
        print_usage(
            result.usage.prompt_tokens,
            result.usage.completion_tokens,
            result.usage.total_tokens,
            result.usage.reasoning_tokens,
        )

from ariadne.cli.main import build_parser, extract_free_prompt


def test_parser_run() -> None:
    p = build_parser()
    args = p.parse_args(["run", "hello", "world"])
    assert args.command == "run"
    assert args.prompt == ["hello", "world"]


def test_parser_exec_alias() -> None:
    p = build_parser()
    args = p.parse_args(["exec", "hello"])
    assert args.command == "exec"
    assert args.prompt == ["hello"]


def test_parser_skills_and_flags() -> None:
    p = build_parser()
    args = p.parse_args(["--skills-dir", "/tmp/skills", "--eager-tools", "skills"])
    assert args.command == "skills"
    assert str(args.skills_dir) == "/tmp/skills"
    assert args.eager_tools is True


def test_parser_stream_and_sandbox() -> None:
    p = build_parser()
    args = p.parse_args(
        [
            "--stream",
            "--sandbox",
            "docker",
            "--sandbox-lifecycle",
            "active_session",
            "--toolbox",
            "docs",
            "run",
            "ping",
        ]
    )
    assert args.stream is True
    assert args.sandbox == "docker"
    assert args.sandbox_lifecycle == "active_session"
    assert args.toolbox == "docs"
    assert args.command == "run"


def test_parser_toolbox_cmd() -> None:
    p = build_parser()
    args = p.parse_args(["toolbox"])
    assert args.command == "toolbox"


def test_parser_bare_entry() -> None:
    p = build_parser()
    args = p.parse_args([])
    assert args.command is None
    args = p.parse_args(["-c", "--no-stream"])
    assert args.command is None
    assert args.continue_last is True
    assert args.no_stream is True


def test_parser_resume() -> None:
    p = build_parser()
    args = p.parse_args(["resume"])
    assert args.command == "resume"
    assert args.session_id is None
    args = p.parse_args(["resume", "--last"])
    assert args.last is True
    args = p.parse_args(["resume", "sess-1"])
    assert args.session_id == "sess-1"


def test_extract_free_prompt() -> None:
    argv, prompt = extract_free_prompt([])
    assert argv == [] and prompt is None

    argv, prompt = extract_free_prompt(["doctor"])
    assert argv == ["doctor"] and prompt is None

    argv, prompt = extract_free_prompt(["run", "hi"])
    assert argv == ["run", "hi"] and prompt is None

    argv, prompt = extract_free_prompt(["hello", "world"])
    assert argv == [] and prompt == "hello world"

    argv, prompt = extract_free_prompt(["--verbose", "fix", "the", "bug"])
    assert argv == ["--verbose"] and prompt == "fix the bug"

    argv, prompt = extract_free_prompt(["--session", "demo", "do", "work"])
    assert argv == ["--session", "demo"] and prompt == "do work"

    argv, prompt = extract_free_prompt(["-c"])
    assert argv == ["-c"] and prompt is None

    argv, prompt = extract_free_prompt(["--", "literal", "prompt"])
    assert argv == [] and prompt == "literal prompt"

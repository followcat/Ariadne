from ariadne.cli.main import build_parser


def test_parser_run() -> None:
    p = build_parser()
    args = p.parse_args(["run", "hello", "world"])
    assert args.command == "run"
    assert args.prompt == ["hello", "world"]


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

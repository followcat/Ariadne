from ariadne.cli.main import build_parser


def test_parser_run() -> None:
    p = build_parser()
    args = p.parse_args(["run", "hello", "world"])
    assert args.command == "run"
    assert args.prompt == ["hello", "world"]

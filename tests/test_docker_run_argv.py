"""Hardened docker run argv — always runs without a daemon."""

from pathlib import Path

from ariadne.sandbox.docker_config import DockerSandboxConfig, build_run_argv


def test_build_run_argv_security_flags(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    sess = tmp_path / "sess"
    ws.mkdir()
    sess.mkdir()
    cfg = DockerSandboxConfig(image="python:3.13-slim-bookworm", network="none")
    argv = build_run_argv(name="ariadne-test", workspace=ws, session_dir=sess, config=cfg)
    joined = " ".join(argv)
    assert argv[0] == "docker" and argv[1] == "run"
    assert "--network" in argv and "none" in argv
    assert "--cap-drop" in argv and "ALL" in argv
    assert "--security-opt" in argv and "no-new-privileges:true" in argv
    assert "--memory" in argv and "512m" in argv
    assert "--cpus" in argv
    assert "--pids-limit" in argv and "128" in argv
    assert "--user" in argv and "1000:1000" in argv
    assert "--read-only" in argv
    assert "--tmpfs" in argv
    assert f"{ws}:/workspace:rw" in joined
    assert f"{sess}:/session:rw" in joined
    assert "--privileged" not in argv
    assert "docker.sock" not in joined
    assert argv[-2:] == ["sleep", "infinity"]


def test_build_run_argv_runtime_and_bridge(tmp_path: Path) -> None:
    cfg = DockerSandboxConfig(
        image="img",
        network="bridge",
        runtime="runsc",
        read_only_rootfs=False,
        labels={"ariadne.scope": "x"},
    )
    argv = build_run_argv(
        name="n",
        workspace=tmp_path / "w",
        session_dir=tmp_path / "s",
        config=cfg,
    )
    (tmp_path / "w").mkdir()
    (tmp_path / "s").mkdir()
    argv = build_run_argv(
        name="n",
        workspace=tmp_path / "w",
        session_dir=tmp_path / "s",
        config=cfg,
    )
    assert "bridge" in argv
    assert "--runtime" in argv and "runsc" in argv
    assert "--read-only" not in argv
    assert "ariadne.scope=x" in " ".join(argv)


def test_build_run_argv_main_readonly_mount(tmp_path: Path) -> None:
    ws = tmp_path / "branch-ws"
    main = tmp_path / "main-ws"
    sess = tmp_path / "sess"
    ws.mkdir()
    main.mkdir()
    (main / "keep.txt").write_text("from-main\n", encoding="utf-8")
    sess.mkdir()
    cfg = DockerSandboxConfig(image="python:3.13-slim-bookworm")
    argv = build_run_argv(
        name="ariadne-br",
        workspace=ws,
        session_dir=sess,
        config=cfg,
        main_readonly=main,
    )
    joined = " ".join(argv)
    assert f"{ws.resolve()}:/workspace:rw" in joined
    assert f"{main.resolve()}:/main-readonly:ro" in joined

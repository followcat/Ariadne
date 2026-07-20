"""Out-of-process entry: ``python -m ariadne.memory.worker_main``.

Used by ``spawn_worker_process`` and hosts that want a standalone drain worker
without loading the full CLI agent stack.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path


def _build_memory(data_dir: Path):
    from .curated import CuratedStore
    from .facade import MemoryFacade
    from .projection import ProjectionWorker
    from .semantic import SemanticIndex
    from .state import ConversationStateStore
    from .summary import TurnSummaryStore
    from .transcript import TranscriptStore

    data_dir.mkdir(parents=True, exist_ok=True)
    mem_root = data_dir / "memory"
    mem_root.mkdir(parents=True, exist_ok=True)
    state = ConversationStateStore(path=mem_root / "state.json")
    return MemoryFacade(
        transcript=TranscriptStore(path=data_dir / "sessions" / "worker.jsonl"),
        curated=CuratedStore(path=mem_root / "curated.json"),
        state=state,
        summaries=TurnSummaryStore(path=mem_root / "summaries.json"),
        semantic=SemanticIndex(path=mem_root / "semantic.json"),
        projection=ProjectionWorker(
            path=mem_root / "projection_jobs.json", state_store=state
        ),
    )


async def _async_main(args: argparse.Namespace) -> int:
    from .worker import MemoryWorker

    memory = _build_memory(Path(args.data_dir))
    worker = MemoryWorker(memory=memory)
    if args.loop:
        n = await worker.run_loop(
            interval_seconds=args.interval,
            max_iterations=args.max_iterations,
            stop_when_idle=args.stop_when_idle,
        )
        print(f"memory-worker-main: iterations={n}")
        return 0
    result = await worker.run_once()
    print(
        "memory-worker-main: "
        f"summaries={result['summaries_processed']} "
        f"projection={result['projection_count']} "
        f"pending_summaries={result['pending_summaries']} "
        f"pending_projection={result['pending_projection']}"
    )
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m ariadne.memory.worker_main",
        description="Out-of-process Ariadne memory worker (summaries + projection)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        required=True,
        help="Ariadne data dir (contains memory/ and sessions/)",
    )
    parser.add_argument("--once", action="store_true", default=False)
    parser.add_argument("--loop", action="store_true", default=False)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--stop-when-idle", action="store_true", default=False)
    parser.add_argument("--max-iterations", type=int, default=None)
    args = parser.parse_args(argv)
    if not args.loop:
        args.once = True
    raise SystemExit(asyncio.run(_async_main(args)))


if __name__ == "__main__":
    main()

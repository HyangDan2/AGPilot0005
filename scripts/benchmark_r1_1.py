"""Phase R1.1 Section 5: Performance & Reliability Benchmark (Legacy vs LangGraph R1, Mock Gateway)."""
from __future__ import annotations

import asyncio
import json
import platform
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.clock import SystemClock
from app.runtime.graph import model_node, tool_node
from app.runtime.langgraph_r1 import LangGraphR1Runtime
from app.runtime.policy.constraints import ConstraintSpec
from app.runtime.state import AgentState
from app.schemas.gateway import FunctionCall, GatewayMessage, ToolCall
from app.tracing.async_writer import AsyncTrajectoryWriter
from app.tracing.store import TrajectoryEventStore, TrajectoryLogger


class FastMockGateway:
    def __init__(self):
        self.step = 0
        self.last_usage = {"input_tokens": 80, "output_tokens": 30, "total_tokens": 110}

    async def execute(self, **kwargs):
        self.step += 1
        if self.step == 1:
            return None, [ToolCall(id="tc_bench_1", function=FunctionCall(name="system.echo", arguments='{"message":"bench"}'))], "tool_calls"
        return "benchmark ok", None, "stop"


def _percentile(values, p):
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round((p / 100.0) * (len(s) - 1)))))
    return round(s[idx] * 1000.0, 2)


async def run_one_legacy(idx: int, constraints: ConstraintSpec) -> float:
    t0 = time.monotonic()
    st = AgentState(
        run_id=f"bench-leg-{idx}",
        service_id=constraints.service_id,
        owner_id=constraints.owner_id,
        messages=[GatewayMessage(role="user", content="bench")],
        max_steps=constraints.max_steps,
        max_tool_calls=constraints.max_tool_calls,
        status="running",
        custom_client=FastMockGateway(),
    )
    while st.step_count < st.max_steps and st.status == "running":
        m = await model_node(st)
        for k, v in m.items():
            setattr(st, k, v)
        if st.pending_tool_calls:
            t = await tool_node(st, constraints=constraints)
            for k, v in t.items():
                setattr(st, k, v)
        else:
            break
    return time.monotonic() - t0


async def run_one_r1(idx: int, constraints: ConstraintSpec, tlogger: TrajectoryLogger) -> float:
    t0 = time.monotonic()
    st = AgentState(
        run_id=f"bench-r1-{idx}",
        service_id=constraints.service_id,
        owner_id=constraints.owner_id,
        messages=[GatewayMessage(role="user", content="bench")],
        max_steps=constraints.max_steps,
        max_tool_calls=constraints.max_tool_calls,
        status="running",
        custom_client=FastMockGateway(),
    )
    rt = LangGraphR1Runtime(trajectory_logger=tlogger, clock=SystemClock())
    await rt.execute(st, constraints)
    return time.monotonic() - t0


async def main() -> None:
    constraints = ConstraintSpec.from_runtime(
        service_id="hd2ai-admin",
        owner_id="bench-user",
        authenticated=True,
        allowed_tools=["system.echo"],
        max_steps=5,
        max_tool_calls=4,
        time_budget_seconds=30.0,
        context_budget_tokens=4000,
        check_live_tool_policy=False,
    )
    report = {
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "gateway_mode": "FastMockGateway (in-process, excludes provider network latency)",
        },
        "scenarios": {},
    }

    for conc in (1, 10, 50):
        # Legacy
        tracemalloc.start()
        leg_latencies = await asyncio.gather(*[run_one_legacy(i, constraints) for i in range(conc)])
        _, leg_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        # LangGraph R1 + AsyncTrajectoryWriter
        with tempfile.TemporaryDirectory() as td:
            store = TrajectoryEventStore(Path(td) / f"bench_{conc}.db")
            writer = AsyncTrajectoryWriter(store, queue_capacity=2048)
            tlogger = TrajectoryLogger(store, clock=SystemClock(), async_writer=writer)
            tracemalloc.start()
            r1_latencies = await asyncio.gather(*[run_one_r1(i, constraints, tlogger) for i in range(conc)])
            flush_res = writer.flush(timeout_seconds=10.0)
            _, r1_peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            health = writer.health()
            writer.shutdown(timeout_seconds=5.0)

        report["scenarios"][f"concurrent_{conc}"] = {
            "sample_size": conc,
            "legacy": {
                "p50_ms": _percentile(leg_latencies, 50),
                "p95_ms": _percentile(leg_latencies, 95),
                "p99_ms": _percentile(leg_latencies, 99),
                "peak_memory_kb": round(leg_peak / 1024.0, 1),
            },
            "langgraph_r1": {
                "p50_ms": _percentile(r1_latencies, 50),
                "p95_ms": _percentile(r1_latencies, 95),
                "p99_ms": _percentile(r1_latencies, 99),
                "peak_memory_kb": round(r1_peak / 1024.0, 1),
                "dropped_events": health["dropped_event_count"],
                "persistence_failures": health["persistence_failure_count"],
                "persisted_events": flush_res.persisted_count,
            },
        }

    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())

"""Capture a synthetic MCP failure in the actual terminal UI as SVG frames.

Run with the Python environment that has Agent Observatory installed:
    python scripts/capture_investigation_video.py --output /tmp/ao-capture
No receiver, API key, network connection, or user session records are read.
"""

import argparse
import asyncio
from pathlib import Path

from render_terminal_demo import FakeFeed, ObservatoryApp, event, now, snapshot


def prepare_failure():
    session = snapshot.local_status["sessions"][0]
    session["project"] = "sample/payments-api"
    session["task"] = "Investigate checkout failure in pull request #184"
    session["model"] = "gpt-6-sol"

    failure = event(
        7, "mcp", "call", "github / get_pull_request",
        {
            "arguments": {"repository": "sample/payments-api", "pull_number": 184},
            "response": {"error": "API rate limit exceeded", "status_code": 403},
        },
        result="error", mcp_server="github", mcp_tool="get_pull_request",
        span_id="span-mcp-demo", duration_ms=713,
    )
    failure["time_unix_nano"] = now - 1_000_000_000
    snapshot.activity = [
        event(1, "message", "user", "Why is checkout failing in PR #184?"),
        event(2, "model", "call", "gpt-6-sol"),
        event(3, "shell", "run", "pytest tests/checkout -q",
              {"command": "pytest tests/checkout -q", "exit_code": 1,
               "output": "1 failed: test_checkout_total"}, result="error"),
        failure,
    ]
    snapshot.activity.sort(key=lambda item: item["time_unix_nano"], reverse=True)
    session["last_activity"] = failure
    snapshot.local_status["sessions"] = snapshot.local_status["sessions"][:3]

    call = snapshot.mcp[0]
    call.update({
        "time_unix_nano": failure["time_unix_nano"],
        "server": "github", "tool": "get_pull_request", "status": "ERROR",
        "duration_ms": 713, "method": "tools/call", "source": "MCP span",
        "arguments": {"repository": "sample/payments-api", "pull_number": 184},
        "output": {"error": {"status_code": 403,
                             "message": "API rate limit exceeded"}},
    })
    snapshot.mcp = [call, snapshot.mcp[1], snapshot.mcp[3], snapshot.mcp[5]]
    for other in snapshot.mcp[1:]:
        other["status"] = "OK"
        other["output"] = {"status": "completed"}
    snapshot.mcp.sort(key=lambda item: item["time_unix_nano"], reverse=True)
    snapshot.mcp_summary = {"servers": [
        {"server": "github", "calls": 2, "tools": 2, "errors": 1},
        {"server": "filesystem", "calls": 1, "tools": 1, "errors": 0},
        {"server": "browser", "calls": 1, "tools": 1, "errors": 0},
    ]}

    root, model, mcp = snapshot.detail["spans"]
    root["status"] = "ERROR"
    root["name"] = "agent.run · investigate checkout"
    root["attributes"]["agent.task"] = session["task"]
    model["name"] = "chat gpt-6-sol"
    mcp["status"] = "ERROR"
    mcp["name"] = "github.get_pull_request"
    mcp["attributes"].update({
        "mcp.server.name": "github", "mcp.tool.name": "get_pull_request",
        "http.response.status_code": 403,
        "error.message": "API rate limit exceeded",
    })
    snapshot.traces[0].update({"task": session["task"], "status": "ERROR"})
    snapshot.totals = {"input_tokens": 12840, "output_tokens": 2140}
    snapshot.sessions[0].update({"input_tokens": 1840, "output_tokens": 326})


async def capture(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    prepare_failure()
    app = ObservatoryApp(FakeFeed())
    async with app.run_test(size=(170, 46)) as pilot:
        await pilot.pause(0.4)
        app.query_one("#brand").update("◈  OBSERVATORY   /   SYNTHETIC CASE")
        for page in ("agents", "activity", "mcp", "traces"):
            app.action_page(page)
            await pilot.pause(0.5)
            (output / f"{page}.svg").write_text(app.export_screenshot())
    print(f"Captured four synthetic UI screens in {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(capture(args.output))

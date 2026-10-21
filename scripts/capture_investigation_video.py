"""Capture a synthetic MCP failure in the actual terminal UI as SVG frames.

Run with the Python environment that has Agenturm installed:
    python scripts/capture_investigation_video.py --output /tmp/ao-capture
No receiver, API key, network connection, or user session records are read.
"""

import argparse
import asyncio
from pathlib import Path

from render_terminal_demo import FakeFeed, ObservatoryApp, event, now, snapshot, source_id


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
        event(2, "message", "user", "Check the related GitHub issue before changing the calculation.", "Check the related GitHub issue before changing the calculation."),
        event(1, "message", "user", "Why is checkout failing in PR #184?", "Why is checkout failing in PR #184?"),
        event(3, "model", "call", "gpt-6-sol"),
        event(4, "shell", "run", "pytest tests/checkout -q",
              {"command": "pytest tests/checkout -q", "exit_code": 1,
               "output": "1 failed: test_checkout_total"}, result="error"),
        failure,
    ]
    snapshot.activity.sort(key=lambda item: item["time_unix_nano"], reverse=True)
    session["last_activity"] = failure
    snapshot.local_status["sessions"] = snapshot.local_status["sessions"][:3]
    session.update(run_state='waiting', waiting_for='Tool response: GitHub rate limit')
    snapshot.local_status['sessions'][1].update(runtime='OpenCode', service='opencode', model='local-model', run_state='running')
    snapshot.local_status['sessions'][2].update(runtime='Claude Code', service='claude', model='claude-sonnet', run_state='completed')

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


async def capture(output: Path, screenshots: Path | None = None, readme_image: Path | None = None,
                  size: tuple[int, int] = (148, 46)):
    output.mkdir(parents=True, exist_ok=True)
    if screenshots or readme_image:
        import cairosvg
    if screenshots:
        screenshots.mkdir(parents=True, exist_ok=True)
    if readme_image:
        readme_image.parent.mkdir(parents=True, exist_ok=True)
    prepare_failure()
    app = ObservatoryApp(FakeFeed())
    app.demo_mode = True
    async with app.run_test(size=size) as pilot:
        await pilot.pause(0.5)
        app.update_layout()
        async def save(name):
            await pilot.pause(0.6)
            svg = app.export_screenshot(title='Agenturm · synthetic demo')
            (output / f'{name}.svg').write_text(svg)
            if screenshots or (readme_image and name == 'agents'):
                svg = svg.replace('Fira Code', 'DejaVu Sans Mono').replace('FiraCode', 'DejaVu Sans Mono')
                png = cairosvg.svg2png(bytestring=svg.encode(), output_width=1920)
                if screenshots:
                    (screenshots / f'{name}.png').write_bytes(png)
                if readme_image and name == 'agents':
                    readme_image.write_bytes(png)
        await save('agents')
        app.selected_source = source_id
        await pilot.press('s')
        await pilot.pause(0.3)
        await pilot.click('#prompts-activity')
        await save('prompts')
        await pilot.press('3')
        await save('mcp')
        await pilot.press('e')
        await pilot.pause(2.2)
        await save('mcp-detail')
        await pilot.press('t')
        await save('traces')
        app.action_page('agents')
        app.action_all()
        await pilot.resize_terminal(80, 24)
        await save('compact')
    print(f'Captured six synthetic terminal states in {output}')



if __name__ == "__main__":
    def terminal_size(value):
        columns, rows = value.lower().split("x", 1)
        return int(columns), int(rows)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--screenshots", type=Path, help="Also export PNG screenshots (requires CairoSVG)")
    parser.add_argument("--readme-image", type=Path, help="Also export the agents overview as a README PNG")
    parser.add_argument("--size", type=terminal_size, default=(148, 46),
                        help="terminal size as COLUMNSxROWS (default: 148x46)")
    args = parser.parse_args()
    asyncio.run(capture(args.output, args.screenshots, args.readme_image, args.size))

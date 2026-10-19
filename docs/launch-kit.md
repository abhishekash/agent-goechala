# Agent Goechala launch kit

## Product and first use

**Agent Goechala:** a local terminal monitor for AI agent activity.
It discovers local Codex and Claude Code session records and receives OTLP traces,
logs, and metrics from instrumented agents. It shows sessions, tasks, MCP calls,
recorded tool inputs/results, tokens, errors, and host processes.

```bash
uvx --from git+https://github.com/abhishekash/agent-goechala agent-goechala
```

Requires uv and Python 3.11+. A machine with no supported session records needs
a Codex/Claude Code task or telemetry from an instrumented agent before it can
show activity. The README GIF uses synthetic records. Available details depend
on the producer's logs and instrumentation.

- [Repository and first-minute guide](https://github.com/abhishekash/agent-goechala)
- [Direct demo GIF](https://raw.githubusercontent.com/abhishekash/agent-goechala/main/media/goechala-demo-preview.gif)
- [PyPI](https://pypi.org/project/agent-goechala/)
- [First-run feedback](https://github.com/abhishekash/agent-goechala/issues/new?template=first_run.yml)
- [SpanHound: MCP trace investigation](https://github.com/abhishekash/spanhound)

## Show HN draft — not submitted

**Title:** Show HN: Agent Goechala – watch local coding agents in your terminal

**URL:** https://github.com/abhishekash/agent-goechala

**First comment:**

I built Agent Goechala to see what my coding agents are doing across sessions:
the current task, shell commands, MCP calls, tool results, tokens, and errors.
It reads local Codex and Claude Code session records as they change and also
accepts OpenTelemetry from instrumented agents. The data is stored in local
SQLite, with a keyboard-first terminal interface. A small HTTP receiver accepts telemetry locally.

Try it with `uvx --from git+https://github.com/abhishekash/agent-goechala agent-goechala`. It works best
on a machine where you already use Codex or Claude Code. The README animation uses synthetic example data; your terminal view
shows what your local records or telemetry actually contain.

This is an early MIT-licensed project. I would like feedback on missing tool
activity, confusing session states, and the effort needed to find a failed call.

The [Show HN guidelines](https://news.ycombinator.com/showhn.html) call for a
working project people can try and a maker who can discuss it. Share the runnable
project, be available for questions, and do not solicit votes.

## LinkedIn draft — not posted

I’ve made two tools for local AI agent observability available:

Agent Goechala shows what coding agents are doing in a terminal: sessions, tasks, MCP calls, recorded tool results, tokens, and errors.
It discovers Codex and Claude Code session records and accepts OpenTelemetry
from instrumented agents.

Try: `uvx --from git+https://github.com/abhishekash/agent-goechala agent-goechala`
https://github.com/abhishekash/agent-goechala

SpanHound (formerly MCP Trace Inspector) lets an MCP client investigate local
trace files: failed tools, recorded approvals, token usage, and differences
between runs. It can also capture supported Python library calls or receive
OTLP/HTTP traces. Its demo uses a bundled scripted regression trace.

Try: `uvx spanhound demo`
https://github.com/abhishekash/spanhound

Both are open source under MIT. I’m looking for people to try them on their own
agent workflows and tell me which debugging questions remain hard to answer.

#OpenTelemetry #MCP #AIAgents #OpenSource

Attach the Goechala GIF or SpanHound's captioned video. Lead with one concrete
debugging task when adapting this copy for another community.

## Launch order and measurement

1. Share one product with an audience that already has the problem: Goechala
   for people running coding agents, SpanHound for people investigating trace files
   through MCP. Use the matching demo and install command.
2. Collect first-run feedback and fix the most common setup or interpretation issue.
3. Share the resulting concrete example with the next relevant audience.

Before and after each launch, compare GitHub unique visitors and referring sites,
new issues with real usage reports, and stars. Download and clone totals can
include automation; they do not establish how many people used a tool.

### Baseline captured 2026-10-15

The GitHub traffic API returned the following totals for its last 14 days:

| Project | Unique visitors | Clones / unique cloners | Stars |
|---|---:|---:|---:|
| Agent Goechala | 0 | 84 / 2 | 0 |
| SpanHound | 37 | 1006 / 368 | 0 |

These counts include our own activity and potential bots. The Goechala
repository has just become public, so this is a starting snapshot.

SpanHound is already on PyPI and the MCP Registry. Its
[awesome-mcp-servers submission](https://github.com/punkpeye/awesome-mcp-servers/pull/16323)
was still open when checked on this date. No duplicate submission is needed.

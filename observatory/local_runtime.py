"""Local host and known agent process metadata; no environment or arguments."""
from __future__ import annotations

import os
import platform
import socket
import time
from pathlib import Path


def snapshot():
    result = {"host": socket.gethostname(), "os": platform.system(), "architecture": platform.machine(),
              "collector_pid": os.getpid(), "processes": [], "process_inventory_available": Path('/proc').is_dir()}
    try:
        result["load_average"] = list(os.getloadavg())
    except (OSError, AttributeError):
        pass
    if not result["process_inventory_available"]:
        return result
    allowed = {"codex", "claude", "claude-code", "opencode", "gemini", "ollama", "cursor"}
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            name = (proc/'comm').read_text().strip()
            if name.lower() not in allowed:
                continue
            fields = {}
            for line in (proc/'status').read_text().splitlines():
                key, _, value = line.partition(':')
                fields[key] = value.strip()
            stat = (proc/'stat').read_text().rsplit(')', 1)[1].split()
            ticks = os.sysconf('SC_CLK_TCK')
            info = {"pid": int(proc.name), "name": name, "state": fields.get('State', ''),
                    "parent_pid": int(fields.get('PPid', 0)),
                    "memory_rss_kib": int(fields.get('VmRSS', '0 kB').split()[0]),
                    "cpu_seconds": round((int(stat[11])+int(stat[12]))/ticks, 2)}
            try:
                info['project'] = Path(os.readlink(proc/'cwd')).name
            except OSError:
                pass
            result['processes'].append(info)
        except (OSError, ValueError, IndexError):
            continue
    result['processes'] = sorted(result['processes'], key=lambda item:item['pid'])[:50]
    result['observed_at_ns'] = time.time_ns()
    return result

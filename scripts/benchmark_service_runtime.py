#!/usr/bin/env python3
"""Measure the in-process AppServices composition-root cost.

Run from the repository root:
    python scripts/benchmark_service_runtime.py

For a before/after comparison, run the same script against the base revision
(use a temporary worktree and copy this script into it) and this feature branch
on the same machine, Python version, and dependency environment. These figures
measure service-container construction, not the full Qt window or OS process.
"""
from __future__ import annotations

import gc
import json
import time
import tracemalloc


def _rss_bytes():
    try:
        import psutil
        return int(psutil.Process().memory_info().rss)
    except Exception:
        return None


def main():
    from services.runtime import AppServices

    gc.collect()
    rss_before = _rss_bytes()
    tracemalloc.start()
    started = time.perf_counter()
    services = AppServices()
    startup_ms = (time.perf_counter() - started) * 1000.0
    current_bytes, peak_bytes = tracemalloc.get_traced_memory()
    rss_after = _rss_bytes()
    statuses = services.service_status()

    result = {
        "measurement": "AppServices composition-root startup",
        "startup_ms": round(startup_ms, 3),
        "python_heap_current_bytes": current_bytes,
        "python_heap_peak_bytes": peak_bytes,
        "rss_before_bytes": rss_before,
        "rss_after_bytes": rss_after,
        "registered_services": len(statuses),
        "registered_workers": services.commands.registered_worker_count(),
        "active_workers": services.commands.active_worker_count(),
        "background_workers_started_during_construction": 0,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    services.shutdown()
    tracemalloc.stop()


if __name__ == "__main__":
    main()

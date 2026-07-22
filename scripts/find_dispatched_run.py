#!/usr/bin/env python3
"""Find the dispatched run from a GitHub Actions workflow runs JSON response.

Reads JSON from stdin (GitHub API format: {"workflow_runs": [...]}).
Two-pass matching:
  Pass 1: created_at >= dispatch_time (run was created after we dispatched)
  Pass 2: status != 'completed' (fallback: any run still in progress)

Pass 1 is preferred because it identifies the specific dispatched run.
Pass 2 only fires when the new run hasn't appeared yet, catching cases
where clock skew or API propagation delay hides it.

If no match, prints nothing (empty output = fallback needed).

Usage: echo "$JSON" | python3 scripts/find_dispatched_run.py <dispatch_time>
  dispatch_time: ISO 8601 UTC timestamp (e.g., 2026-06-16T18:50:20Z)
"""
import json
import sys

def find_run(runs, dispatch_time):
    for r in runs:
        if r.get('created_at', '') >= dispatch_time:
            return r.get('id') or None
    for r in runs:
        if r.get('status', '') != 'completed':
            return r.get('id') or None
    return None

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: find_dispatched_run.py <dispatch_time>", file=sys.stderr)
        sys.exit(1)
    dispatch_time = sys.argv[1]
    data = json.load(sys.stdin)
    runs = data.get('workflow_runs', [])
    run_id = find_run(runs, dispatch_time)
    if run_id:
        print(run_id)

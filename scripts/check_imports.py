#!/usr/bin/env python3
"""CI guard: import every Python module in the repo root and scripts/.

Definition-greps miss cross-file breakage: session 2 removed NOTION_API
from write_notion.py and scripts/remediate_notion.py: which imported
that constant: broke invisibly until a manual import sweep caught it.
This makes the sweep a permanent gate (user-approved 2026-07-04).

Each module is loaded with importlib (spec_from_file_location +
exec_module). SystemExit is OK (argparse-at-import); any other exception
fails, naming the module. Exit 0 = all import; 1 = failures.
"""
import importlib.util
import sys
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKIP_DIRS = {"archive", "tests", "__pycache__", ".git", "docs"}


def modules_to_check():
    yield from sorted(REPO.glob("*.py"))
    yield from sorted((REPO / "pipeline").glob("*.py"))
    yield from sorted((REPO / "scripts").glob("*.py"))


def _is_repo_module(top_level: str) -> bool:
    return (REPO / f"{top_level}.py").exists() or (REPO / top_level).is_dir()


def main() -> int:
    failures = []
    skipped = []
    checked = 0
    baseline_path = list(sys.path)
    for path in modules_to_check():
        rel = path.relative_to(REPO)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        # Reproduce `python3 <path>` cold-start semantics: sys.path gets the
        # SCRIPT'S OWN DIRECTORY, not the repo root, and no repo module is
        # pre-cached. Without this, a scripts/*.py that imports erlib above
        # its sys.path shim (or without one) imports cleanly here but dies
        # at runtime: two QA scripts shipped exactly that way (2026-07-05).
        sys.path[:] = [str(path.parent)] + baseline_path
        for cached in [m for m in sys.modules if m.split(".")[0] == "erlib"]:
            del sys.modules[cached]
        name = f"_import_check_{rel.stem}"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except SystemExit:
            pass  # argparse or deliberate exit at import: module loaded far enough
        except ModuleNotFoundError as e:
            # A missing EXTERNAL package (e.g. google-api-python-client on
            # the CI runner) is an environment gap, not repo breakage.
            # Missing REPO modules still fail: as does "cannot import name"
            # (plain ImportError), which is the incident class this guards.
            top = (e.name or "").split(".")[0]
            if top and not _is_repo_module(top):
                skipped.append(f"{rel} (needs external package {top!r})")
            else:
                failures.append(f"{rel}:\n{traceback.format_exc(limit=3)}")
        except Exception:
            failures.append(f"{rel}:\n{traceback.format_exc(limit=3)}")
        checked += 1

    for s in skipped:
        print(f"  SKIP {s}")
    if failures:
        print(f"Import check FAILED: {len(failures)} module(s) do not import:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"Import check OK ({checked} modules, {len(skipped)} skipped for absent external deps).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

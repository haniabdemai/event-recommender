#!/usr/bin/env python3
"""CI guard: fail when a banned duplicated pattern reappears outside erlib/.

Why this exists: the 2026-07-04 audit (docs/audits/2026-07-04-refactor-audit.md)
found the same helpers forked 2-6 times across the repo (6 Notion clients,
5 _TYPO_TABLE copies, ~25 DB-path constants ...). Each fork drifts and
re-manufactures bugs. The refactor consolidated them into erlib/; this guard
stops any session: human or model: from quietly starting a new fork.

Patterns are appended as WP3 migrates each private copy. If this guard blocks
you: import the helper from erlib instead of redefining it.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# (pattern-name, compiled regex, paths where it IS allowed (prefix match))
BANNED = [
    # Populated progressively by refactor WP3 tasks as each copy is eliminated.
    # WP3.3: all Notion access goes through erlib.notion; the endpoint
    # constant lives only in erlib.config. Import it, don't redefine it.
    ("notion-api-const", re.compile(r"^NOTION_API\s*="), ("erlib/",)),
    # WP3.4: TYPO_TABLE/normalise_name live only in erlib.normalise.
    ("private-typo-table", re.compile(r"^_?TYPO_TABLE\s*="), ("erlib/",)),
    # WP3.7: Google OAuth refresh lives in erlib.google_auth.
    ("oauth-refresh", re.compile(r"def refresh_access_token"), ("erlib/",)),
    # WP3.7: the Borderline cap is one constant in erlib.constants.
    ("borderline-cap", re.compile(r"BORDERLINE_CAP\s*="), ("erlib/",)),
    # WP3.8: the DB path is erlib.config.DB_PATH; import it (aliasing to
    # DEFAULT_DB is fine), don't re-derive it.
    ("default-db-const", re.compile(r"^DEFAULT_DB\s*=\s*[^;]*event-recommender\.db"), ("erlib/",)),
    # WP3.8: duplicate detection lives in erlib.dedup only.
    ("is-duplicate-fork", re.compile(r"^def is_duplicate\("), ("erlib/",)),
    # Phase 2 (2026-07-12): every Notion HTTP call goes through
    # erlib.notion.NotionClient: a raw transport needs this header.
    ("private-notion-transport", re.compile(r"Notion-Version"), ("erlib/",)),
    # The ntfy topic is erlib.config.NTFY_TOPIC (env-driven); scripts import
    # it. A hardcoded topic string after ntfy.sh/ is a private-copy smell:
    # only ${NTFY_TOPIC} interpolation is allowed outside erlib.
    ("ntfy-topic-literal", re.compile(r"ntfy\.sh/(?!\$\{NTFY_TOPIC\})[a-z0-9-]{4,}"), ("erlib/",)),
    # Phase 2 (2026-07-12): the rich_text cap + ellipsis truncation live in
    # erlib.notion (NOTION_TEXT_LIMIT, truncate_with_ellipsis).
    ("notion-text-limit-const", re.compile(r"^NOTION_TEXT_LIMIT\s*="), ("erlib/",)),
    # Phase 2 (2026-07-12): the '30 Apr' label formatter is erlib.dates.batch_label.
    ("batch-label-fork", re.compile(r"^def batch_label\("), ("erlib/",)),
]

# Deliberately NOT banned (WP3 closeout decision, 2026-07-04):
# - raw sqlite3.connect: ~20 files still open the DB directly with a path
#   argument. It is 2-line boilerplate, not drift-prone logic; migrating all
#   call sites to erlib.db.connect is deferred to Phase 2.
# - a generic iso-date-parser regex would false-positive on any legitimate
#   fromisoformat use; erlib.dates is enforced by review instead.

# Pattern used only by the self-test to prove the machinery works.
SELF_TEST_PATTERN = ("self-test-marker", re.compile(r"^DUPLICATION_GUARD_SELF_TEST_MARKER\s*="), ("erlib/",))

SCAN_SUFFIXES = {".py", ".sh"}
SKIP_DIRS = {"archive", "__pycache__", ".git", "docs", "tests"}
SKIP_FILES = {"check_no_duplication.py"}


def scan(banned) -> list[str]:
    hits = []
    for path in sorted(REPO.rglob("*")):
        if path.suffix not in SCAN_SUFFIXES or not path.is_file():
            continue
        rel = path.relative_to(REPO)
        if any(part in SKIP_DIRS for part in rel.parts) or rel.name in SKIP_FILES:
            continue
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for name, rx, allowed in banned:
            if any(str(rel).startswith(a) for a in allowed):
                continue
            for i, line in enumerate(lines, 1):
                if rx.search(line):
                    hits.append(f"{rel}:{i} banned pattern '{name}'")
    return hits


def main() -> int:
    banned = BANNED + [SELF_TEST_PATTERN]
    hits = scan(banned)
    if hits:
        print("Duplication guard FAILED: these belong in erlib/ (see docs/audits/2026-07-04-refactor-audit.md):")
        print("\n".join(hits))
        return 1
    print(f"Duplication guard OK ({len(BANNED)} live patterns).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

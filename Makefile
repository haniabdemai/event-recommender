PY := python3

.PHONY: test lint check-fast check smoke bash-tests pytest-tests import-check

# Every module's offline self-test suite. If you add a module with a
# --smoke-test flag, add it here: CI runs `make test` on every PR.
# sync_verdicts gains a suite in refactor WP2.2; newsletter_tracker in WP4.2.
smoke:
	$(PY) pipeline/score_candidates.py --smoke-test
	$(PY) pipeline/fetch_meetup.py --smoke-test
	$(PY) pipeline/fetch_luma.py --smoke-test
	$(PY) pipeline/llm_sense_check.py --smoke-test
	$(PY) pipeline/validate_llm_output.py --smoke-test
	$(PY) pipeline/dedup_candidates.py --smoke-test
	$(PY) pipeline/enrich_descriptions.py --smoke-test
	$(PY) pipeline/merge_multiday.py --smoke-test
	$(PY) pipeline/verify_event_dates.py --smoke-test
	$(PY) pipeline/write_notion.py --smoke-test
	$(PY) pipeline/reconcile_notion.py --smoke-test
	$(PY) pipeline/travel_time.py --smoke-test
	$(PY) pipeline/sync_to_gcal.py --smoke-test
	$(PY) pipeline/feedback_digest.py --smoke-test
	$(PY) pipeline/sync_verdicts.py --smoke-test
	$(PY) seed_demo_data.py --smoke-test
	$(PY) scripts/bootstrap_notion.py --self-test
	$(PY) scripts/setup_location.py --smoke-test
	$(PY) scripts/bootstrap_taste_profile.py --smoke-test
	$(PY) tests/test_validate_llm_output.py
	$(PY) tests/test_check_no_duplication.py
	$(PY) tests/test_check_prompt_contract.py
	$(PY) tests/test_erlib_config.py
	$(PY) tests/test_erlib_db.py
	$(PY) tests/test_erlib_notion.py
	$(PY) tests/test_erlib_normalise.py
	$(PY) tests/test_erlib_dates.py
	$(PY) tests/test_erlib_dedup.py
	$(PY) tests/test_erlib_run_log.py
	$(PY) tests/test_newsletter_tracker.py
	$(PY) tests/test_generate_postmortem.py

bash-tests:
	bash tests/test_write_status.sh
	bash tests/test_health_check.sh
	bash tests/test_pipeline_status.sh
	bash tests/test_dispatch_poll.sh
	bash tests/test_validator_gate.sh
	bash tests/test_pipeline_summary_golden.sh
	bash tests/test_write_ready.sh
	bash tests/test_record_newsletters.sh

lint:
	ruff check .

# Fast gate (<10s): used by the pre-commit session hook and CI's first step.
check-fast: lint import-check
	$(PY) scripts/check_no_duplication.py
	$(PY) scripts/check_prompt_contract.py
	$(PY) scripts/check_veto_sync.py

# Imports every root + scripts/ module; catches cross-file breakage that
# definition-greps miss (remediate_notion/NOTION_API incident, session 2).
import-check:
	$(PY) scripts/check_imports.py

# pytest wrappers (tests/test_smoke_wrappers.py) subsume the smoke target
# with per-suite reporting; smoke stays for direct operator use.
pytest-tests:
	pytest tests/ -x -q

test: pytest-tests bash-tests

check: check-fast test

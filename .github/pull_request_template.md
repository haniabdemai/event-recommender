# Definition of done

- [ ] `make test` green locally (paste the tail or state the count)
- [ ] CI green on the PR head
- [ ] Frozen interfaces intact: `scripts/check_prompt_contract.py` passes
- [ ] Docs updated for any interface or behaviour change (CLAUDE.md, README, setup guide)
- [ ] `event-recommender.db` is NOT part of this PR (pipeline workflows are the only DB writers)
- [ ] If `references/scheduled-task-prompt.md` changed: live-trigger sync noted here and scheduled (sync once, after all prompt edits are final)

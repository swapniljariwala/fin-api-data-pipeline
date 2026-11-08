# Open questions — integrated filings

Decisions that need a human. Asked one at a time; the answer is written next to the
question when given. Longer-running work items are in the `## TODO` section of
[`AGENTS.md`](AGENTS.md).

Status: `[ ]` open, `[x]` answered.

1. [x] **fin-api handoff note.** Write a short note for the fin-api agent (latest-release rule,
   join on `instrument_id`, use `object_path` as stored, discover statements per filing, page
   `related_party_transactions`, null means "not reported")?
   Answer: Yes. Written to `FIN_API_HANDOFF.md` (2026-11-08).
2. [ ] **Orphan cleanup.** After the prod re-key is verified, delete the old shared-folder objects
   in the bucket (`filings/{symbol}/{fy}/{quarter}/{basis}/{statement}.json`, old `canonical.json` /
   `_manifest.json`)? Needs the verification report first. 7-day soft delete is the safety net.
   Answer:
3. [x] **Fiscal-quarter labels.** Paths use the Indian April-March `fy`/`quarter` for every company.
   For companies with a calendar or Jul-Jun year (31 filings, 16 companies so far) this differs from the
   company's own fiscal quarter. Keep as is (the mismatch is recorded in the manifest), or add the
   company's own `fy`/`quarter` to the manifest / table?
   Answer: Keep the Indian April-March labels for now; storing the company's own fiscal quarter is in the TODO in `AGENTS.md` (2026-11-08).
4. [x] **Library issues to track.** Add to the TODO: (a) HDFC Life Dec-2025 filing with a mis-written
   reporting period blanks most of `canonical()`; (b) optional mapping gaps: bank `total_expenses`,
   GI `total_income` / `total_expenses`, bank and insurer `total_equity` (needs a sum)?
   Answer: Yes, both added to the TODO in `AGENTS.md` (2026-11-08).
5. [ ] **Prod housekeeping.** (a) rotate `logs/cron.log` (20 MB, unrotated)? (b) `chmod 600
   /home/swapnil/service-account.json` (currently readable by all users)? (c) put
   `ingest_corporate_actions` on cron (it is not scheduled; DEPLOYMENT.md has a suggested entry)?
   Answer:
6. [ ] **Filings NSE does not serve.** 8 are HTTP 404 and 1 is an empty file on NSE's archive. Leave as
   `failed`, or retry them automatically after some days in case NSE republishes?
   Answer:
7. [ ] **ISIN-less instrument.** NAHARENTP (4 filings) has no ISIN in our instrument master. Leave it
   (it fixes itself when the master gets the ISIN) or fix now?
   Answer:
8. [ ] **Push.** Push the unpushed docs commits to `origin/main`?
   Answer:

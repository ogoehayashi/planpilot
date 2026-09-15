# Component smoke evidence

Run `python tools/run_smoke_harness.py` to exercise a small deterministic set of
component checks against the compact JSON fixture. The artifact uses lowercase
smoke statuses and `check_id` fields so it cannot be mistaken for formal EVAL
case evidence.

`related_contract_cases` exists only for traceability. A green smoke check does
not execute or satisfy the referenced contract acceptance case. Formal status
remains `PENDING_UNTIL_EVAL_001_TO_030_EXECUTE` until dedicated end-to-end case
runners produce independently inspectable evidence.

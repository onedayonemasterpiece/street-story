# Editorial utilization acceptance

`full_social` stays the short whole-path smoke, including its explicit two-fact
owner request and retained Telegram publication. It does not measure utilization
of a rich inventory.

Run `tools.editorial_utilization_acceptance` separately on an already identified
story with at least eight eligible, source-backed facts. Use the installed backend
environment and one managed retained artifact directory:

```sh
dev-artifacts new street-story editorial-utilization --retain --reason "Pending editorial acceptance"
PYTHONPATH=backend python -m tools.editorial_utilization_acceptance \
  --source-db /home/dev/.local/state/street-story/data/street-story.sqlite3 \
  --story-id story_ID --output /home/dev/artifacts/street-story/MANAGED_TASK
```

The existing shared guarded Live host receives natural owner requests. The first
asks Mira to choose several substantively different facts, devise another angle,
and save the selection, concept and draft. The second asks for another post about
the same object without unnecessarily repeating the prior angle. Neither names
facts nor prescribes a concept. Previous story selections/concepts come from
existing exact-POI story history; they are context, not new fact authority or an
automatic selection. No recommendation store or topic classifier is introduced.

The source store is read-only. Only the addressed identity and eligible fact
ledger are copied into the existing product test-store fixture. Production story,
post, auth data and runtime queues are untouched. No media is copied. Independent
fixture stories preserve the source claim IDs, exact text, qualifications and
source passages. They do not claim another identity acceptance.

Receipts record the complete whole-claim inventory delivered in the successful
Live setup plus any actual paginated `get_facts` responses. A truncated setup
requires further model reads. Both scenarios must save more than two eligible
facts and preserve their selected IDs. The second may use different facts and
concept; novelty is not a deterministic gate.

The final visual request lets Mira choose a small subset of the rich selection.
The real adapter validates and queues it in the test store. There is **no worker,
image generation or Telegram send**. Check the subset, retained draft/concept
and unchanged full owner selection. Unknown production operations are never
resumed by this harness.

Mechanical success returns `REVIEW_REQUIRED`, never semantic PASS from counts
alone. Inspect the two drafts against their selected claims and exact source
passages. Write an assessment with two `scenarios`, each containing the exact
`selected_fact_ids`, `concept`, `draft`, `source_backed_diverse_selection`,
`draft_uses_only_selected_facts` and an explanatory `reason`. Then:

```sh
PYTHONPATH=backend python -m tools.editorial_utilization_acceptance \
  --output /home/dev/artifacts/street-story/MANAGED_TASK \
  --assess-receipt /home/dev/artifacts/street-story/MANAGED_TASK/editorial-RUN/acceptance.json \
  --assessment /home/dev/artifacts/street-story/MANAGED_TASK/semantic-assessment.json
```

This is bounded text/editorial runtime acceptance. Physical microphone, Android
UI, actual image quality and Telegram delivery remain separate evidence from
the existing publication smoke and owner phone testing.

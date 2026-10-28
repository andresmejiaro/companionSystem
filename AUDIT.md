Fable-ready semantic consistency audit
The audit ran with a Sol Thinking lead and two gpt-5.6-terra reviewers. No Anthropic model was used.
Production was accessed read-only. No prompt, memory, store, forum, or production file was modified.
Snapshot and coverage
Source: root@62.238.55.207:/var/lib/docker/volumes/profile-os_profile-os-data/_data
A local snapshot was captured under /tmp/profile-os-production-audit on 2026-08-14 at approximately 09:48 CEST.
Database SHA-256: 839ffbe6338b3f096623bad568902a717fd51be1092e10b401e24cc4d92aa30e
SQLite integrity check: ok
19 companion profiles
114 logical current prompt paths: 111 regular files plus 3 symlinks
258 historical prompt-version files, used only for temporal classification
19 compact states
301 memory events
40 dynamic-store definitions
670 dynamic records
230 database closeouts
18 closeouts.jsonl files containing the same 230 closeout IDs
50 companion-owned files
Reviewed sources included profile metadata, current prompts, compact state, memories, structured stores and records, closeouts, and relevant companion-owned files. Generic game manuals and exam-question banks were structurally counted and filtered because they do not assert facts about Andrés or a companion. No auth or credential rows were queried.
Confirmed semantic inconsistencies
Type	Incompatible assertions	Provenance
Static prompt conflict	Hilo must append to fewshot_hilo.md at closeout, while another current prompt forbids normal-operation writes to it.	hilo/what_you_do.md:5; hilo/how_you_keep_context.md:8
Current lane conflict	LT Rita metadata says “Strategy, not drilling,” while current prompts say “Drill and narrate. That’s the job” and define the lane as exam drilling.	profiles.id=lt_rita.description; lt_rita/what_you_do.md:1; lt_rita/lane.md:1
Variant identity/lane conflict	Red Vértice’s symlinked identity declares it to be Vértice, the career-strategy companion, while its own current prompt says it receives briefs from Vértice and does not hold strategy. The inherited signature also differs from Red Vértice’s current writing signature.	red_vertice/who_you_are.md -> ../vertice/who_you_are.md:1; red_vertice/what_you_do.md:1–5,16; red_vertice/signature.md:1; split confirmed by closeout 040d6770-770c-4ef8-85aa-57cf3844d82c
Stale current-state assertion	Noctámbulo says “Stores — none currently,” but an approved noctambulo_continuity store contains 23 records.	noctambulo/how_you_keep_context.md:7; store 9e963c06-cc81-46cb-8be5-47cba27443ec; 23 current records
Derived-count conflict	Noctámbulo compact state says there are 16 stories in the store; current records contain 15 story rows, 7 sessions, and 1 state row.	compact_state:noctambulo, updated 2026-08-13T23:04:50Z; direct count through 2026-08-13T23:04:38Z
Derived-count conflict	Miga’s current prompt says the recetas store has 14 recipes; the store currently contains 16 records.	miga/how_you_keep_context.md:3; direct dynamic_records count; latest additions d50d6ea4-342a-486f-8fd3-c33ca8cb637e and 103c99c8-cd60-4746-8831-e76c9edfe483
Current itinerary conflict	Hilo schedules a full Granada activity day on 2026-09-20, while Cruce still schedules departure from Madrid around 23:00 that day and Granada lodging from 2026-09-21. The later Hilo record may represent an unpropagated itinerary revision, but the current stores remain incompatible.	Hilo 4d113ef7-3f86-48ad-881a-d599324ddd97; Cruce 29032e6d-c5ed-4bd8-8e47-ed4049adb14e; lodging d2bc70d3-eb4c-4db5-9988-d17f5ab2382e


These findings identify incompatible current assertions only. They do not establish which assertion is true.
Ambiguous candidates
Tara’s prompt gives Andrés’s weight as “around 87 kg”; Rumbo’s latest measurement is 84.1 kg on 2026-08-12. This is likely stale live-value framing, but “around” has no defined tolerance. Evidence: tara/what_you_do.md:18; Rumbo record 24072fcf-94ef-4393-917a-4b6b6a0b7e3f.

Cruce compact state says eight buses remain to reserve, while seven movimientos rows explicitly contain PENDIENTE RESERVAR. The store does not state that it exhaustively contains every bus, so the missing eighth row cannot be inferred. Evidence: compact_state:cruce, updated 2026-08-09T17:11:15Z; seven matching transport records.

Rejected false positives
The Sol comparison rejected:
Dated clinical gates versus later recovery results: temporal change.
Mapfre versus out-of-pocket psychologist records: explicit later supersession, although a stale Dr Vera clone remains marked active.
Earlier versus later game momentum, training progression, and deployment state: temporal/live-state changes.
Aster “handles” architecture versus not owning its governance: scope/ownership framing.
Rita assessment snapshots and Red Vértice canonical-copy rules: different measurements or predicates.
Dr Vera’s conditional progressive recovery versus Preen’s later “closed/loading heavily” language: compatible progression framing.
LT Rita and Rita’s CCDV-F date: independently consistent at 2026-09-10.
Exact read and reduction actions
Ran a read-only SSH find inventory on the production volume.
Streamed profile_os.db and profiles/ through SSH/tar into /tmp; nothing was created remotely.
Computed hashes and timestamps, ran SQLite integrity checking, inspected schema and relevant row counts.
Assigned disjoint profile sets to two Terra agents:A: aster, cruce, dr_vera, hilo, lt_rita, lumenis, noctambulo, preen, vera
B: aster_gm, limo, miga, red_vertice, rita, rumbo, tara, tool_probe, vertice, vesper

Terra used profile-scoped find/rg, read-only SQLite queries, nl -ba, and jq, producing normalized claims and provenance-bearing candidates.
Sol JSON-validated and cross-sorted 19 high-signal cross-profile claims, directly requeried every final candidate, checked temporal ordering, and resolved three current prompt symlinks missed by regular-file enumeration.
Only local /tmp artifacts were written:/tmp/terra_shard_a_report.md
/tmp/terra_shard_a_claims.jsonl
/tmp/terra_shard_a_candidates.jsonl
/tmp/terra_shard_b_report.md
/tmp/terra_shard_b_claims.jsonl
/tmp/terra_shard_b_candidates.jsonl

Limitations
The snapshot was integrity-valid but was copied from a live SQLite file rather than a transactionally frozen production export. Production may have changed after capture. File modification times were not treated as semantic dates. Numerical nutrition, exercise, exam, and game calculations were not independently recomputed unless needed to verify an explicit count conflict.
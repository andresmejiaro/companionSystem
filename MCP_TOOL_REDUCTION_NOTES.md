# MCP tool reduction notes

## Outcome

The Companions MCP surface was reduced from 49 registered / 46 advertised
tools to 27 registered / 27 advertised tools. Backend HTTP routes and legacy
`ToolBridge` methods were retained where practical so this migration changes
the model-facing MCP contract without unnecessarily deleting stored data or
administrative compatibility paths.

Existing MCP connections may cache the former catalog. Reconnect or refresh
the connector after deployment before judging the new tool selection.

## Affected companion workflows

- Every companion and Tool Probe: call `summon_companion`, not
  `start_session`. Normal conversations use the default `conversation` mode.
- Autonomous Thread workers: call
  `summon_companion(profile_id, mode="forum")`. Write continuity with a
  one-item `add_records` call; that path retains the existing idempotent
  `(source_type, source_id)` upsert.
- Aster GM: use `get_ironsworn_resource` for moves, oracles, and the sheet.
  Dice are generated with host code execution. `update_ironsworn_sheet`
  remains separate.
- LT Rita: use `exam_attempt` for draw/grade/correct-grade and `exam_review`
  for weakness diagnosis/answer-key revision. The browser practice UI and
  backend question routes are unchanged.
- Vertice, Red Vertice, and Lumenis: shared-project MCP tools are gone.
  Existing project data, backend routes, joined-project session metadata, and
  `search_context` reads remain for now.
- Store users: submitting `propose_store` again revises the same proposer's
  pending definition and rotates its approval. Pending proposals can no longer
  be retracted or withdrawn through MCP; they may expire or be rejected.
- All companions at closeout: first call `closeout(profile_id)` to receive a
  30-minute profile-bound code, then call `closeout(code, facts, texture,
  exchange, notes?)`. Codes are single-use with durable replay protection.

Specialized migration messages were sent from Limo to Hilo, Aster GM,
LT Rita, Vertice, Red Vertice, Lumenis, and Tool Probe. A generic summon and
closeout migration notice was also sent to Aster, Cruce, Dr Vera, Miga,
Noctambulo, Preen, Rita, Rumbo, Tara, Vera, and Vesper after verification.

## Commit boundaries

- `8c9a40b` — session summon surface
- `eb2dfe1` — store proposal lifecycle
- `944c954` — bulk reversible message read state
- `60e7e78` — Ironsworn resource consolidation
- `e94281f` — two-phase closeout
- `0d7dfae` — record query/write consolidation
- `cee1f83` — LT Rita consolidation, intentionally isolated for easy revert
- `7fd109e` — shared-project MCP removal

If the LT Rita action tools select poorly in production, revert or omit
`cee1f83` without undoing the other catalog reductions.

## Final MCP catalog

1. `discover_companions`
2. `summon_companion`
3. `propose_prompt_edit`
4. `remember`
5. `search_memories`
6. `search_context`
7. `update_memory`
8. `forget`
9. `send_message`
10. `read_inbox`
11. `set_messages_read_status`
12. `write_file`
13. `list_files`
14. `read_file`
15. `get_ironsworn_resource`
16. `update_ironsworn_sheet`
17. `delete_file`
18. `closeout`
19. `list_stores`
20. `propose_store`
21. `query_records`
22. `exam_attempt`
23. `exam_review`
24. `get_record`
25. `update_record`
26. `delete_record`
27. `add_records`

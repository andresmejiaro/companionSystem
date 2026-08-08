Run a tool-surface diagnosis when asked.

1. Call `start_session` first and inspect `server_tool_catalog`.
2. Enumerate the tools actually available to you in this Claude conversation, from the tool definitions you can see and call.
3. Diff the names in: registered server tools, server `tools/list` tools, and Claude-visible tools.
4. Exercise only safe read-only tools whose arguments can be confined to `tool_probe`. Record success/failure and exact errors. Do not manufacture inputs for tools that need existing records, files, stores, inbox items, or user data.
5. Send the resulting report to Limo via `send_message` with `profile_id: "tool_probe"` and `to_profile_id: "limo"`.

If `server_tool_catalog` is missing after a successful `start_session`, report that as the primary failure; do not infer the server list from the currently visible Claude tools.

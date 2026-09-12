### New Features

- **Project MCP servers:** Load workspace `.mcp.json` configurations after explicit approval. Manage local and remote servers, trust decisions, and authentication with `y2 mcp` commands without opening the interactive shell.
- **Final response JSON:** Read the completed final answer from `final_output` in `y2 ask --json` while retaining accumulated assistant text in `output`.
- **Active-turn guidance:** Press Ctrl+Enter to steer an active turn at its next model boundary. Enter keeps a prompt queued for the next turn.
- **Request instructions:** Use `--system` to replace the base prompt for one request while retaining project instructions, tools, and skills.

### Improvements

- **Catalog navigation:** Browse help, models, settings, skills, and saved sessions in compact terminal menus. Both `/model` and `/models` open the model picker.
- **Direct endpoint usage:** Track reported token and request counts in the native CLI, including separately streamed totals received within one second of completion. Missing totals and unreported prices remain marked incomplete.
- **MCP lifecycle:** Configure server startup timeouts and clean up owned Docker containers after shutdown or a failed start.

### Bug Fixes

- **Agent y2 tool execution:** Send repository instructions, conversation history, and local tool definitions to Agent y2 so the harness can carry out approved work on your machine.
- **Stream recovery:** Recover when a native request receives no response headers, and retain completed answers when trailing usage metadata is missing, late, or malformed.
- **Markdown rendering:** Preserve wrapped lists, fenced code, links, emphasis, Unicode punctuation, and literal backslashes more consistently.
- **SDK session lists:** Return every page of native sessions while preserving workspace scope, including relative workspace paths.
- **MCP sign-in:** Allow MCP sign-in without a client secret when the server advertises S256 PKCE, even if it omits `none` from its authentication methods.
- **Credential recovery:** Keep a compatible explicitly selected credential source in status and doctor guidance when no credential resolves.

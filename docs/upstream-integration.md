# Upstream integration and Y2 compatibility

This integration follows the requirement to retain all existing Y2 interfaces and adapt compatible upstream updates. It does not claim that the latest upstream tree has been integrated in full.

## Source checkpoints

| Role | Commit |
| --- | --- |
| Existing Y2 main | `1b5d6c9b` |
| Shared ancestor | `cca8be57` |
| Upstream merge boundary | `9cffee561b624a89c166b7570c1cd7e95908dac1` |
| Upstream tip reviewed for compatibility | `95b567af7c6f9ff079bb0b665b39864326bbacfd` |

The merge boundary is the last upstream main commit before utility removals. Its compatible changes include project MCP configuration and explicit trust, MCP management from the CLI, runtime and terminal recovery, inline catalog menus, request-specific system prompts, and separate final-response JSON output. Integration adapts those changes to the existing Y2 contracts.

Later commits were reviewed individually for compatibility. The reviewed tip contains broader utility and SDK redesigns that cannot be imported unchanged while retaining the current interfaces. An upstream commit appearing in the reviewed range is not evidence that its behavior has shipped.

## Additional compatible changes

| Source | Adaptation |
| --- | --- |
| `deef88876cf8658f0b59fb9d1a16e43cce41f7cf` from Y2 PR 6, clarified by `cb8b171511c9e020f26f87e775b4fc5f1daca7ec` | When no credential resolves, preserve a compatible explicitly selected source in recovery guidance. Existing credential selection and fallback remain unchanged; text and JSON diagnostics are verified. |
| `09ff1684ffa7bc3c18bbf3bde3655eb0cffc5e7a` | Permit secretless PKCE OAuth when server metadata omits the `none` authentication method. |
| `3fbb457073e9a5dc3c81fbcce0dfb308dd00510c` | Indent wrapped lines of literal bullet lists. |
| `80afb1790a55ec8006b6a5efe1756d21636a9bc2` | Match GFM block syntax for fences, lists, headings, and quotes. |
| `43dd7d27419caea571a2b5eb1ac29373aaeef401` | Close tab indented fences and keep plus lines and backslashes literal. |
| `2b679271481efdd6a1871b61e2f8536569cbb76a` | Keep heading backslashes inside blockquotes. |
| `85389f7b6bceb9623fe55615b9fcb8bb8c7638a1` | Parse GFM link destinations, code span runs, and entities. |
| `b14d394080b8fd0541be33d93e79be9d51c325fb` | Keep control character entities literal and bound entity lookup. |
| `ea74b7f6ef61c9c6070e98d9e05517c6a3500988` | Resolve inline emphasis with a delimiter stack. |
| `90416597b80a35a3aed3b1982678b32ade071d31` | Match emphasis on an explicit opener stack and decode flanking neighbours. |
| `01e382d3b03d72f6b0295381675dd0be46c00a18` | Classify flanking by Unicode category and cache failed residual searches. |
| `6049fcf2389418d033bf3177d7ef4477a595609a` | Give the tokenizer sole ownership of flanking and bound bracket scans. |
| `44a669cea8b2cad4c958918d1e70f750311c3324` | Share the next closing bracket across bracket candidates. |

This table records selected changes applied beyond the merge boundary. It does not claim that every later upstream change was imported or that the reviewed upstream tip is an ancestor of this integration.

## Preserved compatibility boundaries

- The executable remains `y2`, the profile remains `~/.y2`, and project defaults remain `.y2.json`. Existing Y2 environment variables, profile state, authentication, and session data retain their identities.
- Agent Y2 remains the default route at `https://api.y2.dev/api/v1/chat/completions` with model `y2-agent`. Client function tools execute locally under Y2 permissions. Direct OpenAI-compatible endpoints, their isolated credentials, model discovery, images, and Codex/Grok subscription routes remain supported.
- Existing CLI commands, slash commands, tools, and aliases remain available. `/models` is a compatibility alias for the shared `/model` picker. The top-level `y2 models` command retains text and JSON output. Existing terminal, background-process, filesystem, memory, and subagent interfaces are retained.
- Existing JSON fields remain available. The `final_output` field for `y2 ask --json` and MCP inspection fields extend their existing snapshots.
- The `liby2` package, `createY2Agent`, `createY2Terminal`, Y2 WebAssembly imports, and existing session-oriented JavaScript APIs retain their names and contracts. The newer upstream ephemeral SDK API is not substituted for them.
- Native SDK `listSessions()` retains its workspace scope and follows ACP cursor pages internally, returning the same array API. WebAssembly lists the host store. Repeated or invalid cursors fail instead of silently truncating results or looping.
- The Y2 logo, Information Dominance welcome text, API documentation links, installer, upgrade endpoint, and canonical `y2-intel/harness` repository identity remain in place.
- The source version remains `0.0.7`; this integration does not prepare a new release. Stable archives and checksums retain Y2 names. Apple Developer ID signing remains deferred, and publication does not regain dependencies on the former upstream CDN.

## Compatibility adaptations

- Preserve native SDK workspace scope and collect every ACP session-list page behind the existing `listSessions()` array API. Keep relative workspace paths and immutable request events supported.
- Record native direct endpoint token counts through a typed reported-usage outcome. Endpoint-scoped response identities prevent collisions; unknown prices remain incomplete, and durable publication remains idempotent. Native requests allow one second after completion for a separate usage trailer. Missing or late trailers retain the completed answer and leave usage incomplete. SDK host transports keep returning at completion and do not collect a separately delayed trailer.
- Bound native response-header acquisition to 30 seconds or an earlier request deadline so a silent server enters the existing recovery flow. This header deadline does not cap the duration of the streamed answer.
- Keep the original complete terminal start schema and explicit write-lease forms alongside the new atomic input form. Retain the existing `mcp_search_tools` advertisement alongside additive capability discovery.

## Validation requirements

Focused checks for the changed behavior, a successful build, and a real interaction with the freshly built `./zig-out/bin/y2` are required before claiming runtime success. Tests that use fixtures do not establish live API or deployed installer acceptance.

Release, installer, and public-surface checks run from the repository root:

```sh
python3 -m unittest scripts.tests.test_macos_signing -q
sh scripts/test-install.sh
./scripts/check-public-surface.sh
zig fmt --check src/
git diff --check
```

The public-surface script checks tracked sensitive data; it does not alone verify branding. Review the package manifests, public exports, build artifact names, release workflows, installer URLs, profile paths, and rendered startup banner against the preserved boundaries above.

Run focused CLI, MCP, direct endpoint, session, terminal, and menu coverage for the affected paths. New or retained E2E owners must have exactly one classification in `scripts/pgso/corpus.json` and matching CI shard coverage.

The SDK pagination regression is covered in existing SDK owners:

```sh
node sdk/tests/test-liby2-loader.mjs
zig build -Dnapi-surface=core
node sdk/tests/test-native-core.mjs
zig build -Dwasm-surface=core
node --experimental-wasm-jspi sdk/tests/test-core.mjs
```

Use Node.js 24 for these SDK checks. The loader test exercises cursor progression, an empty middle page, repeated and invalid cursors, stable captured event data, and host-store behavior. The native test creates more than one page of sessions and verifies explicit, default, and relative workspace isolation.

A draft PR must remain a draft until Full CI passes for the exact current commit on Linux x86_64, Linux arm64, macOS x86_64, and macOS arm64, followed by the final ship gate. Query PR checks with `gh pr checks --repo y2-intel/harness`; a successful result from another commit does not satisfy this gate.

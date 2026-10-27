### Bug Fixes

- **Workspace threads:** Agent Y2 requests now carry the local session id and client identity, so every step of a session lands in one y2.dev workspace thread that is marked as written by the harness instead of creating a new conversation per step.

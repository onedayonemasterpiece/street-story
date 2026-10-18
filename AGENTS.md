# Street Story agent instructions

Before changing any integration between Street Story, RKB, VibePublish or
another service on the same DevCoveer host, follow the
[canonical local S2S policy](https://github.com/onedayonemasterpiece/codex-devcoveer-mcp/blob/main/docs/operations/local-service-connections.md) and the
[devserver-interservice skill](https://github.com/onedayonemasterpiece/codex-devcoveer-mcp/blob/main/skills/devserver-interservice/SKILL.md).

Use the existing local HTTP/technical Bearer path for internal service calls;
keep credentials per service and bound by exact endpoints/destinations.
Never forward user/Android tokens or switch to browser OAuth/public HTTPS
for local background work. Preserve durable idempotency keys, retry/reconciliation,
provider proofs, and existing source ACL. Do not invent a central connector
or claim live completion from CI alone.

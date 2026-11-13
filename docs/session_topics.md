# Session topics

This optional feature generates a short English task topic for a caller-supplied session. Topics contain at most 50 characters, including spaces and punctuation. They describe the first task rather than every later turn. A topic is an investigation aid, not evidence that a key has or has not been disclosed.

The feature is disabled by default. Enabling it requires an explicit key allowlist, a dedicated inference-only virtual key, a shared identity secret and the new database migration. No production keys are created by the application.

## Data flow

An authenticated business request supplies a stable `litellm_session_id`, `metadata.session_id`, or supported session header. Generated trace identifiers are not treated as real sessions. The reference stored in the database is an HMAC of the organization, team, authenticated key hash and session identifier. Different keys cannot merge sessions by choosing the same identifier.

The request hook extracts only the latest user text. System instructions, assistant history, tool results, images and other attachments are excluded. Code blocks and recognizable credentials, addresses and identifiers are removed before a bounded in-memory task is submitted. Filtering is best-effort. Topics remain sensitive and must have appropriate access controls and retention.

PostgreSQL atomically claims a session with a lease and an attempt counter shared by all workers. Each session permits at most two summary requests, including repair and retry. The summary SDK disables its own retries, and authenticated summary-key requests disable gateway retries and fallbacks. A ready topic is not regenerated. Insufficient input may be retried on a later request while the attempt budget remains.

Raw input is never written to the topic tables or a durable task queue. The queue has 32 slots per worker, two consumers and a 60-second memory deadline. A worker restart can lose pending text. Expired work is not reconstructed from conversation history. Existing business inference does not wait for the summary result. Queue capacity and unassociated-request counters are process-local operational state, not a complete historical audit trail.

## Installation and configuration

Build the fork's Dockerfile, which installs the `session_topics` extra. Source installations should use `uv sync --frozen --extra proxy --extra extra_proxy --extra session_topics` so the Prisma and other runtime versions match the repository lockfile. Pin the resulting image digest. Do not deploy a floating branch or tag.

The canonical Prisma migration is `litellm-proxy-extras/litellm_proxy_extras/migrations/20261113213000_session_topics/migration.sql`. It adds three small tables without rewriting existing request logs. The matching schema is present in all three Prisma schema copies. Apply migrations through the normal release process before enabling the feature.

Configure a dedicated model alias called `session-topic-luna`, pointing to the approved `gpt-6-luna` deployment. Configure its existing provider credentials through the normal secret mechanism. No provider credentials are needed by the topic generator itself. Do not configure automatic fallbacks for this alias.

Create a dedicated virtual key with only that model, `key_type: llm_api`, and explicit positive `max_budget`, `rpm_limit` and `tpm_limit`. Do not use a master key or an employee's key. The feature validates these properties at startup. Place the returned key directly in the secret manager, not in source control, chat or command logs.

Set these environment variables through deployment secrets and configuration.

| Variable | Meaning |
| --- | --- |
| `LITELLM_SESSION_TOPICS_ENABLED` | `true` enables the feature |
| `LITELLM_SESSION_TOPICS_GATEWAY_URL` | Internal LiteLLM URL ending in `/v1` |
| `LITELLM_SESSION_TOPICS_MODEL` | Model alias, default `session-topic-luna` |
| `LITELLM_SESSION_TOPICS_VIRTUAL_KEY` | Dedicated virtual key, secret |
| `LITELLM_SESSION_TOPICS_IDENTITY_SECRET` | Shared random secret of at least 32 characters |
| `LITELLM_SESSION_TOPICS_ALLOWED_KEYS` | Comma-separated authenticated key SHA-256 hashes; empty means no business keys; `*` explicitly enables all |
| `LITELLM_SESSION_TOPICS_PRIOR_WORKER_KEYS` | Optional old summary-key hashes for a rotation overlap |

The identity secret must be identical across workers. Rotating it starts new session references, so retire old references through the retention policy instead of trying to merge them by IP.

Enable `litellm_settings.turn_off_message_logging`, keep `store_prompts_in_spend_logs` false, disable verbose and DEBUG logging, and redact exception content. Verify every configured logging callback, trace exporter, response cache and error handler before rollout. The worker sends `store=false` and no-cache/no-store controls. These controls do not establish the provider's retention policy. Verify the approved Azure or other provider deployment separately. Do not grant client message-redaction opt-out permissions to the summary key.

The default model request uses Chat Completions, strict JSON Schema, `reasoning_effort=none`, a 2,000-token input budget and 256 output tokens. The topic is validated again after generation. Non-English, oversized, malformed or visibly sensitive output is rejected, not truncated. The repair attempt receives the sanitized task and stricter instructions, never the rejected model output.

## Reading topics and usage

Open **Observability / Session topics** in the dashboard. The page shows only the generated topic, category, state, owner, key reference and timestamps. It has no conversation-content drill-down. Paging stays in the URL and credentials are never written to browser local storage by this feature.

`GET /session_topics?limit=50&offset=0` returns topics visible to the authenticated caller. Proxy administrators and read-only proxy administrators can see all records. Verified dashboard sessions can see their owner's records. Other API keys are restricted to their own key records, even when keys share an owner. Existing route-permission checks still apply. An inference-only key does not gain management access. The summary key is explicitly denied access to the topic reader.

`GET /session_topics/overview?days=7` returns retained-session status counts and usage from the existing spend ledger. Business inference, internal summaries and other gateway traffic are reported separately. Classification uses authenticated key hashes, not the model name or caller-provided purpose labels. Summary-key hashes are registered persistently so rotating a key does not reclassify its historical spend as business usage. Regular callers do not receive other users' or the shared summary service's usage.

Reads are audited with an opaque actor reference, operation, count and timestamp. Topic content is not copied into the read audit. Topic records and read audits are purged in bounded batches after 30 days. Summary-key hashes are retained as accounting classification metadata. No raw key is stored in these tables.

The original request ledger remains intact. Revoking a key does not reclassify its historical inference or summary requests. A failed or timed-out summary can still have a provider charge. Do not delete internal summary records or treat a missing usage response as zero cost. The displayed values are LiteLLM-recorded spend, not reconciled provider invoices.

## Verification and rollout

Start with an isolated database and Redis instance. Test two proxy workers, all client API formats in use, streaming, duplicate requests, same session identifiers under different users, invalid keys, missing session identifiers, queue saturation, timeouts and restart. Seed credentials and personal-data canaries, then inspect topic tables, proxy logs, traces and caches for them. Confirm that an internal summary request does not enqueue another topic.

The focused tests live in `tests/unit/litellm_core_utils/test_session_topic_text.py`, `tests/unit/proxy/hooks/test_session_topic_client.py`, `tests/integration/spend/test_session_topics.py` and `ui/litellm-dashboard/src/components/session_topics/SessionTopicsPanel.integration.test.tsx`. The PostgreSQL contract test accepts `SESSION_TOPIC_TEST_DATABASE_URL`, otherwise it uses the integration harness's `DATABASE_URL`, and creates a disposable schema. Windows runs need a selector event loop for the Psycopg async test adapter.

The accounting integration group also runs `tests/integration/spend/test_session_topics_gateway.py`. It starts a private PostgreSQL database and two proxy workers, provisions synthetic virtual keys, and drives Chat Completions, Responses and Messages with and without streaming. A loopback summary relay sends summary calls back through the same proxy's authentication and accounting. The provider replies are scripted, so this verifies gateway behavior but does not replace real-Luna acceptance. All compilation and test execution for this change runs in GitHub Actions.

Validate the actual Luna deployment with synthetic or explicitly approved redacted inputs before any employee traffic is enabled. Verify that the gateway forwards the schema and privacy parameters rather than silently dropping them. Start with a few explicit key hashes, inspect generation failures and cost, and expand only after acceptance.

To roll back, disable `LITELLM_SESSION_TOPICS_ENABLED` and restart normally. The runtime cancels in-memory jobs and closes its own HTTP client before database shutdown. Existing business inference and the original spend ledger are retained. Disabling the feature does not erase already stored topics; use the documented retention process for those records.

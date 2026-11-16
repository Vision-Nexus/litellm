SET LOCAL lock_timeout = '10s';

CREATE TABLE IF NOT EXISTS "LiteLLM_SessionTopic" (
    "session_ref" TEXT PRIMARY KEY,
    "key_hash" TEXT NOT NULL,
    "user_id" TEXT,
    "team_id" TEXT,
    "organization_id" TEXT,
    "topic" VARCHAR(50),
    "category" TEXT,
    "status" TEXT NOT NULL DEFAULT 'pending',
    "attempts" INTEGER NOT NULL DEFAULT 0 CHECK ("attempts" BETWEEN 0 AND 2),
    "model" TEXT NOT NULL,
    "prompt_version" TEXT NOT NULL,
    "lease" TEXT,
    "lease_until" TIMESTAMPTZ,
    "first_seen" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "last_seen" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK ("session_ref" ~ '^[a-f0-9]{64}$'),
    CHECK ("key_hash" ~ '^[a-f0-9]{64}$'),
    CHECK ("topic" IS NULL OR (length("topic") BETWEEN 1 AND 50 AND "topic" !~ '[^ -~]')),
    CHECK ("status" IN ('pending','ready','insufficient_input','invalid_output','upstream_failure','capacity','expired'))
);

CREATE INDEX IF NOT EXISTS "LiteLLM_SessionTopic_user_id_last_seen_idx" ON "LiteLLM_SessionTopic" ("user_id", "last_seen");
CREATE INDEX IF NOT EXISTS "LiteLLM_SessionTopic_key_hash_last_seen_idx" ON "LiteLLM_SessionTopic" ("key_hash", "last_seen");
CREATE INDEX IF NOT EXISTS "LiteLLM_SessionTopic_last_seen_idx" ON "LiteLLM_SessionTopic" ("last_seen");

CREATE TABLE IF NOT EXISTS "LiteLLM_SessionTopicReadAudit" (
    "id" BIGSERIAL PRIMARY KEY,
    "actor_ref" TEXT NOT NULL,
    "operation" TEXT NOT NULL,
    "result_count" INTEGER NOT NULL,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS "LiteLLM_SessionTopicReadAudit_created_at_idx" ON "LiteLLM_SessionTopicReadAudit" ("created_at");

CREATE TABLE IF NOT EXISTS "LiteLLM_SessionTopicWorkerKey" (
    "key_hash" TEXT PRIMARY KEY CHECK ("key_hash" ~ '^[a-f0-9]{64}$'),
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

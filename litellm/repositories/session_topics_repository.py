from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Final, Protocol

from pydantic import BaseModel, ConfigDict, TypeAdapter

from litellm.litellm_core_utils.session_topic_text import hash_key, make_scope, session_id
from litellm.models.session_topics import (
    TOPIC_PROMPT_VERSION,
    TopicAttempt,
    TopicConfig,
    TopicCostKind,
    TopicCostPage,
    TopicCostQuery,
    TopicOverview,
    TopicPage,
    TopicRecord,
    TopicScope,
    TopicState,
    TopicUsage,
    TopicViewer,
    TopicWorkerKey,
)


class TopicDatabase(Protocol):
    async def query_raw(self, query: str, *args: object) -> Sequence[Mapping[str, object]]: ...

    async def execute_raw(self, query: str, *args: object) -> int: ...


_RECORDS: Final = TypeAdapter(tuple[TopicRecord, ...])
_USAGE: Final = TypeAdapter(tuple[TopicUsage, ...])
_VISIBLE: Final = "($1::boolean OR user_id = $2::text OR key_hash = $3::text)"
_OBSERVE: Final = """
INSERT INTO "LiteLLM_SessionTopic"
    (session_ref,key_hash,user_id,team_id,organization_id,model,prompt_version,status)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
ON CONFLICT (session_ref) DO UPDATE SET last_seen=now()
"""
_CLAIM: Final = """
INSERT INTO "LiteLLM_SessionTopic"
    (session_ref,key_hash,user_id,team_id,organization_id,model,prompt_version,status,attempts,lease,lease_until)
VALUES ($1,$2,$3,$4,$5,$6,$7,'pending',1,$8,now()+interval '60 seconds')
ON CONFLICT (session_ref) DO UPDATE
SET attempts="LiteLLM_SessionTopic".attempts+1,status='pending',lease=$8,
    lease_until=now()+interval '60 seconds',last_seen=now(),updated_at=now()
WHERE "LiteLLM_SessionTopic".attempts<2 AND "LiteLLM_SessionTopic".status<>'ready'
    AND ("LiteLLM_SessionTopic".lease_until IS NULL OR "LiteLLM_SessionTopic".lease_until<now())
RETURNING session_ref
"""

_BUSINESS_TYPES: Final = (
    "('acompletion','completion','aresponses','responses','anthropic_messages','_aresponses_websocket')"
)
_COST_STATUS: Final = """CASE
    WHEN s.metadata->>'session_topic_cost_status' IN ('known','unpriced','unknown')
        THEN s.metadata->>'session_topic_cost_status'
    WHEN s.spend <> 0 THEN 'known' ELSE 'unknown' END"""
_LEGACY_LIMIT: Final = 1000


class LegacyCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    key_hash: str
    session_id: str
    team_id: str | None
    organization_id: str | None


class LegacyReference(LegacyCandidate):
    session_ref: str


_LEGACY_CANDIDATES: Final = TypeAdapter(tuple[LegacyCandidate, ...])


def _legacy_reference(candidate: LegacyCandidate, config: TopicConfig) -> LegacyReference | None:
    identity: Final = session_id({"litellm_session_id": candidate.session_id})
    if identity is None:
        return None
    scope: Final = make_scope(
        config.identity_secret.get_secret_value(),
        candidate.key_hash,
        identity,
        None,
        candidate.team_id or None,
        candidate.organization_id or None,
    )
    return LegacyReference(
        key_hash=candidate.key_hash,
        session_id=candidate.session_id,
        team_id=candidate.team_id,
        organization_id=candidate.organization_id,
        session_ref=scope.session_ref,
    )


def _cost_json(kind: TopicCostKind | None = None) -> str:
    predicate: Final = f"s.kind='{kind}'" if kind is not None else "TRUE"
    return f"""jsonb_build_object(
        'requests',count(s.request_id) FILTER(WHERE {predicate}),
        'successful',count(s.request_id) FILTER(WHERE {predicate} AND s.status='success'),
        'failed',count(s.request_id) FILTER(WHERE {predicate} AND s.status='failure'),
        'tokens',coalesce(sum(s.total_tokens) FILTER(WHERE {predicate}),0),
        'recorded_spend',coalesce(sum(s.spend) FILTER(WHERE {predicate}),0)::float8,
        'amount',CASE WHEN count(s.request_id) FILTER(WHERE {predicate} AND s.cost_status<>'known')=0
            THEN coalesce(sum(s.spend) FILTER(WHERE {predicate}),0)::float8 ELSE NULL END,
        'unpriced_requests',count(s.request_id) FILTER(WHERE {predicate} AND s.cost_status='unpriced'),
        'unknown_cost_requests',count(s.request_id) FILTER(WHERE {predicate} AND s.cost_status='unknown'))"""


_LEGACY_QUERY: Final = f"""
SELECT s.api_key AS key_hash,s.session_id,nullif(s.team_id,'') AS team_id,
       nullif(s.organization_id,'') AS organization_id
FROM "LiteLLM_SpendLogs" s
WHERE s."startTime">=(now() at time zone 'UTC')-($4::int*interval '1 day')
  AND s."startTime"<=(now() at time zone 'UTC')
  AND ($1::boolean OR s."user"=$2::text OR s.api_key=$3::text)
  AND s.call_type IN {_BUSINESS_TYPES} AND s.session_id IS NOT NULL
  AND NOT coalesce(s.metadata ? 'session_topic',FALSE)
  AND NOT(s.api_key=ANY($5::text[]))
  AND NOT EXISTS(SELECT 1 FROM "LiteLLM_SessionTopicWorkerKey" w WHERE w.key_hash=s.api_key)
ORDER BY s."startTime" DESC,s.request_id DESC LIMIT {_LEGACY_LIMIT + 1}
"""

_COST_QUERY: Final = f"""
WITH visible_topics AS (
    SELECT * FROM "LiteLLM_SessionTopic" WHERE {_VISIBLE}
), legacy AS (
    SELECT * FROM jsonb_to_recordset($8::jsonb)
        AS l(key_hash text,session_id text,team_id text,organization_id text,session_ref text)
), classified AS (
    SELECT s.*, {_COST_STATUS} AS cost_status,
        CASE WHEN s.api_key=ANY($9::text[])
            OR EXISTS(SELECT 1 FROM "LiteLLM_SessionTopicWorkerKey" w WHERE w.key_hash=s.api_key)
            THEN 'session_topic' WHEN s.call_type IN {_BUSINESS_TYPES} THEN 'business' ELSE 'other' END AS kind
    FROM "LiteLLM_SpendLogs" s
    WHERE s."startTime">=(now() at time zone 'UTC')-($5::int*interval '1 day')
        AND s."startTime"<=(now() at time zone 'UTC')
        AND ($1::boolean OR s."user"=$2::text OR s.api_key=$3::text)
), scoped AS (
    SELECT * FROM classified WHERE $1::boolean OR kind<>'session_topic'
), linked AS (
    SELECT s.*,coalesce(d.session_ref,h.session_ref) AS target_ref,
           d.session_ref IS NULL AND h.session_ref IS NOT NULL AS historical
    FROM scoped s
    LEFT JOIN visible_topics d ON d.session_ref=s.metadata->'session_topic'->>'session_ref'
        AND s.metadata->'session_topic'->>'version'='1'
        AND s.metadata->'session_topic'->>'kind'=s.kind
        AND (s.kind='session_topic' OR (s.kind='business' AND d.key_hash=s.api_key))
    LEFT JOIN legacy l ON NOT coalesce(s.metadata ? 'session_topic',FALSE)
        AND s.kind='business' AND l.key_hash=s.api_key AND l.session_id=s.session_id
        AND coalesce(l.team_id,'')=coalesce(s.team_id,'')
        AND coalesce(l.organization_id,'')=coalesce(s.organization_id,'')
    LEFT JOIN visible_topics h ON h.session_ref=l.session_ref AND h.key_hash=s.api_key
), grouped AS (
    SELECT CASE WHEN $4::text='session' THEN t.session_ref
                WHEN $4::text='key' THEN t.key_hash
                ELSE jsonb_build_array(t.topic,t.category)::text END AS group_key,
        CASE WHEN $4::text='session' THEN min(t.session_ref) ELSE NULL END AS session_ref,
        CASE WHEN $4::text='key' THEN NULL ELSE min(t.topic) END AS topic,
        CASE WHEN $4::text='key' THEN NULL ELSE min(t.category) END AS category,
        CASE WHEN $4::text IN ('session','key') THEN min(t.key_hash) ELSE NULL END AS key_hash,
        CASE WHEN count(DISTINCT coalesce(t.user_id,''))=1 THEN min(t.user_id) ELSE NULL END AS user_id,
        count(DISTINCT t.session_ref) AS sessions,
        count(DISTINCT t.session_ref) FILTER(WHERE t.status='ready') AS ready_sessions,
        max(t.last_seen) AS last_seen,
        coalesce(array_agg(DISTINCT coalesce(nullif(s.model_group,''),s.model))
            FILTER(WHERE s.request_id IS NOT NULL),ARRAY[]::text[]) AS models,
        count(s.request_id) FILTER(WHERE s.historical) AS historical_business_requests,
        {_cost_json("business")} AS business,
        CASE WHEN $1::boolean THEN {_cost_json("session_topic")} ELSE NULL END AS summary,
        CASE WHEN $1::boolean THEN {_cost_json()} ELSE NULL END AS total,
        coalesce(sum(s.spend),0) AS sort_spend
    FROM visible_topics t LEFT JOIN linked s ON s.target_ref=t.session_ref
    WHERE t.last_seen>=now()-($5::int*interval '1 day') OR s.request_id IS NOT NULL
    GROUP BY group_key
), page AS (
    SELECT session_ref,topic,category,key_hash,user_id,sessions,ready_sessions,last_seen,models,
           historical_business_requests,business,summary,total
    FROM grouped ORDER BY sort_spend DESC,last_seen DESC,group_key LIMIT $6 OFFSET $7
), totals AS (
    SELECT s.kind,{_cost_json()} AS costs FROM scoped s GROUP BY s.kind
), unassociated AS (
    SELECT s.kind,{_cost_json()} AS costs FROM linked s WHERE s.target_ref IS NULL GROUP BY s.kind
)
SELECT coalesce((SELECT jsonb_agg(to_jsonb(p)) FROM page p),'[]'::jsonb) AS items,
       (SELECT count(*) FROM grouped) AS total_groups,
       coalesce((SELECT jsonb_agg(to_jsonb(t) ORDER BY kind) FROM totals t),'[]'::jsonb) AS totals,
       coalesce((SELECT jsonb_agg(to_jsonb(u) ORDER BY kind) FROM unassociated u),'[]'::jsonb) AS unassociated
"""


class SessionTopicsRepository:
    def __init__(self, database: Callable[[], TopicDatabase]) -> None:
        self._database = database

    @property
    def database(self) -> TopicDatabase:
        return self._database()

    async def worker_key(self, key_hash: str) -> TopicWorkerKey | None:
        rows: Final = await self.database.query_raw(
            'SELECT models,key_type,max_budget,rpm_limit,tpm_limit FROM "LiteLLM_VerificationToken" WHERE token=$1',
            key_hash,
        )
        return TopicWorkerKey.model_validate(rows[0]) if rows else None

    async def register_worker(self, key_hash: str) -> None:
        await self.database.execute_raw(
            """INSERT INTO "LiteLLM_SessionTopicWorkerKey" (key_hash) VALUES ($1)
               ON CONFLICT (key_hash) DO NOTHING""",
            key_hash,
        )

    async def verify_schema(self) -> None:
        await self.database.query_raw('SELECT session_ref FROM "LiteLLM_SessionTopic" LIMIT 0')

    async def observe(self, scope: TopicScope, model: str, state: TopicState) -> None:
        await self.database.execute_raw(
            _OBSERVE,
            scope.session_ref,
            scope.key_hash,
            scope.user_id,
            scope.team_id,
            scope.organization_id,
            model,
            TOPIC_PROMPT_VERSION,
            state.value,
        )

    async def claim(self, scope: TopicScope, model: str, lease: str) -> bool:
        return bool(
            await self.database.query_raw(
                _CLAIM,
                scope.session_ref,
                scope.key_hash,
                scope.user_id,
                scope.team_id,
                scope.organization_id,
                model,
                TOPIC_PROMPT_VERSION,
                lease,
            )
        )

    async def finish(self, session_ref: str, lease: str, attempt: TopicAttempt) -> None:
        await self.database.execute_raw(
            """UPDATE "LiteLLM_SessionTopic" SET status=$3,topic=$4,category=$5,
               lease=NULL,lease_until=NULL,updated_at=now()
               WHERE session_ref=$1 AND lease=$2""",
            session_ref,
            lease,
            attempt.state.value,
            attempt.result.topic if attempt.result else None,
            attempt.result.category if attempt.result else None,
        )

    async def list_topics(self, viewer: TopicViewer, limit: int, offset: int) -> TopicPage:
        rows: Final = await self.database.query_raw(
            "SELECT session_ref,key_hash,user_id,team_id,organization_id,topic,category,status,attempts,"
            'model,prompt_version,first_seen,last_seen,updated_at FROM "LiteLLM_SessionTopic" WHERE '
            + _VISIBLE
            + " ORDER BY last_seen DESC,session_ref LIMIT $4 OFFSET $5",
            viewer.admin,
            viewer.user_id,
            viewer.key_hash,
            limit,
            offset,
        )
        return TopicPage(items=_RECORDS.validate_python(rows), limit=limit, offset=offset)

    async def overview(self, viewer: TopicViewer, worker_hashes: tuple[str, ...], days: int) -> TopicOverview:
        counts: Final = await self.database.query_raw(
            """SELECT count(*) AS sessions,count(*) FILTER(WHERE status='ready') AS ready,
               count(*) FILTER(WHERE status='pending' AND lease_until>now()) AS pending,
               count(*) FILTER(WHERE status<>'ready' AND (status<>'pending' OR lease_until<=now()
                   OR lease_until IS NULL)) AS unavailable
               FROM "LiteLLM_SessionTopic" WHERE """
            + _VISIBLE,
            viewer.admin,
            viewer.user_id,
            viewer.key_hash,
        )
        usage: Final = await self.database.query_raw(
            f"""SELECT CASE WHEN s.api_key=ANY($4::text[])
                OR EXISTS (SELECT 1 FROM "LiteLLM_SessionTopicWorkerKey" w WHERE w.key_hash=s.api_key)
                THEN 'session_topic'
                WHEN s.call_type IN ('acompletion','completion','aresponses','responses',
                     'anthropic_messages','_aresponses_websocket') THEN 'business'
                ELSE 'other' END AS kind,
                count(*) AS requests,count(*) FILTER(WHERE s.status='success') AS successful,
                count(*) FILTER(WHERE s.status='failure') AS failed,
                count(*) FILTER(WHERE ({_COST_STATUS})='unpriced') AS unpriced_requests,
                count(*) FILTER(WHERE ({_COST_STATUS})='unknown') AS unknown_cost_requests,
                coalesce(sum(s.total_tokens),0) AS tokens,coalesce(sum(s.spend),0)::float8 AS spend
                FROM "LiteLLM_SpendLogs" s
                WHERE s."startTime">=(now() at time zone 'UTC')-($5::int*interval '1 day')
                AND s."startTime"<=(now() at time zone 'UTC')
                AND ($1::boolean OR s."user"=$2::text OR s.api_key=$3::text)
                AND ($1::boolean OR (NOT(s.api_key=ANY($4::text[])) AND NOT EXISTS (
                    SELECT 1 FROM "LiteLLM_SessionTopicWorkerKey" w WHERE w.key_hash=s.api_key)))
                GROUP BY 1 ORDER BY 1""",
            viewer.admin,
            viewer.user_id,
            viewer.key_hash,
            list(worker_hashes),
            days,
        )
        return TopicOverview.model_validate({**counts[0], "usage": _USAGE.validate_python(usage)})

    async def costs(self, viewer: TopicViewer, config: TopicConfig, query: TopicCostQuery) -> TopicCostPage:
        workers: Final = tuple(
            frozenset((hash_key(config.virtual_key.get_secret_value()),)) | config.prior_worker_key_hashes
        )
        candidates: Final = _LEGACY_CANDIDATES.validate_python(
            await self.database.query_raw(
                _LEGACY_QUERY, viewer.admin, viewer.user_id, viewer.key_hash, query.days, list(workers)
            )
        )
        distinct: Final = frozenset(candidates[:_LEGACY_LIMIT])
        recovered: Final = tuple(_legacy_reference(candidate, config) for candidate in distinct)
        mappings: Final = "[" + ",".join(row.model_dump_json() for row in recovered if row is not None) + "]"
        rows: Final = await self.database.query_raw(
            _COST_QUERY,
            viewer.admin,
            viewer.user_id,
            viewer.key_hash,
            query.group_by,
            query.days,
            query.limit,
            query.offset,
            mappings,
            list(workers),
        )
        return TopicCostPage.model_validate(
            {
                **rows[0],
                **query.model_dump(),
                "legacy_scan_limit": _LEGACY_LIMIT,
                "legacy_scan_truncated": len(candidates) > _LEGACY_LIMIT,
                "topic_retention_days": config.retention_days,
            }
        )

    async def audit_read(self, viewer: TopicViewer, operation: str, result_count: int) -> None:
        await self.database.execute_raw(
            """INSERT INTO "LiteLLM_SessionTopicReadAudit" (actor_ref,operation,result_count)
               VALUES ($1,$2,$3)""",
            viewer.actor_ref,
            operation,
            result_count,
        )

    async def purge(self, days: int) -> None:
        await self.database.execute_raw(
            """WITH expired AS (SELECT session_ref FROM "LiteLLM_SessionTopic"
               WHERE last_seen<now()-($1::int*interval '1 day') ORDER BY last_seen LIMIT 1000)
               DELETE FROM "LiteLLM_SessionTopic" t USING expired e WHERE t.session_ref=e.session_ref""",
            days,
        )
        await self.database.execute_raw(
            """WITH expired AS (SELECT id FROM "LiteLLM_SessionTopicReadAudit"
               WHERE created_at<now()-($1::int*interval '1 day') ORDER BY created_at LIMIT 1000)
               DELETE FROM "LiteLLM_SessionTopicReadAudit" t USING expired e WHERE t.id=e.id""",
            days,
        )

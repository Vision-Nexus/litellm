from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Final, Protocol

from pydantic import TypeAdapter

from litellm.models.session_topics import (
    TOPIC_PROMPT_VERSION,
    TopicAttempt,
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
            """SELECT CASE WHEN s.api_key=ANY($4::text[])
                OR EXISTS (SELECT 1 FROM "LiteLLM_SessionTopicWorkerKey" w WHERE w.key_hash=s.api_key)
                THEN 'session_topic'
                WHEN s.call_type IN ('acompletion','completion','aresponses','responses',
                     'anthropic_messages','_aresponses_websocket') AND k.token IS NOT NULL THEN 'business'
                ELSE 'other' END AS kind,
                count(*) AS requests,count(*) FILTER(WHERE s.status='success') AS successful,
                count(*) FILTER(WHERE s.status='failure') AS failed,
                coalesce(sum(s.total_tokens),0) AS tokens,coalesce(sum(s.spend),0)::float8 AS spend
                FROM "LiteLLM_SpendLogs" s LEFT JOIN "LiteLLM_VerificationToken" k ON k.token=s.api_key
                WHERE s."startTime">=(now() at time zone 'UTC')-($5::int*interval '1 day')
                AND ($1::boolean OR k.user_id=$2::text OR s.api_key=$3::text)
                GROUP BY 1 ORDER BY 1""",
            viewer.admin,
            viewer.user_id,
            viewer.key_hash,
            list(worker_hashes),
            days,
        )
        return TopicOverview.model_validate({**counts[0], "usage": _USAGE.validate_python(usage)})

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

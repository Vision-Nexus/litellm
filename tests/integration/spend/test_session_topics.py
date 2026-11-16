import asyncio
import json
import os
import re
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Final

import httpx
import psycopg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from psycopg.rows import dict_row
from pydantic import SecretStr

from litellm.caching.caching import DualCache
from litellm.constants import UI_SESSION_TOKEN_TEAM_ID
from litellm.litellm_core_utils.session_topic_cost import topic_cost_log_metadata
from litellm.litellm_core_utils.session_topic_text import latest_user_text, make_scope
from litellm.models.session_topics import (
    TopicAttempt,
    TopicConfig,
    TopicCostQuery,
    TopicResult,
    TopicScope,
    TopicState,
    TopicViewer,
)
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.proxy.deps.repositories import get_session_topics_repository
from litellm.proxy.hooks.session_topic_client import LunaTopicGenerator
from litellm.proxy.hooks.session_topic_engine import TopicEngine, TopicJob
from litellm.proxy.hooks.session_topics import SessionTopicRuntime
from litellm.proxy.management_endpoints.session_topic_endpoints import router as topics_router
from litellm.repositories.session_topics_repository import SessionTopicsRepository

_MIGRATION: Final = (
    Path(__file__).resolve().parents[3]
    / "litellm-proxy-extras/litellm_proxy_extras/migrations/20261113213000_session_topics/migration.sql"
)


class PostgreSQLQueries:
    def __init__(self, connection: psycopg.AsyncConnection[dict[str, object]]) -> None:
        self.connection = connection

    def statement(self, query: str, args: tuple[object, ...]) -> tuple[str, tuple[object, ...]]:
        positions: Final = tuple(int(match.group(1)) - 1 for match in re.finditer(r"\$(\d+)", query))
        return re.sub(r"\$\d+", "%s", query), tuple(args[index] for index in positions)

    async def query_raw(self, query: str, *args: object) -> Sequence[Mapping[str, object]]:
        statement, values = self.statement(query, args)
        async with self.connection.cursor() as cursor:
            await cursor.execute(statement, values)
            return await cursor.fetchall()

    async def execute_raw(self, query: str, *args: object) -> int:
        statement, values = self.statement(query, args)
        async with self.connection.cursor() as cursor:
            await cursor.execute(statement, values)
            return cursor.rowcount


@pytest_asyncio.fixture
async def queries() -> AsyncIterator[PostgreSQLQueries]:
    url: Final = os.environ.get("SESSION_TOPIC_TEST_DATABASE_URL") or os.environ["DATABASE_URL"]
    schema: Final = "topic_test_" + uuid.uuid4().hex
    async with await psycopg.AsyncConnection.connect(url, autocommit=True, row_factory=dict_row) as conn:
        await conn.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema)))
        await conn.execute(psycopg.sql.SQL("SET search_path TO {}").format(psycopg.sql.Identifier(schema)))
        await conn.execute(_MIGRATION.read_text(encoding="utf-8"))
        await conn.execute("""CREATE TABLE "LiteLLM_VerificationToken" (token text PRIMARY KEY,user_id text);
            CREATE TABLE "LiteLLM_SpendLogs" (
                request_id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
                api_key text,call_type text,status text,total_tokens integer,spend double precision,
                "user" text DEFAULT '',team_id text,organization_id text,session_id text,
                model text DEFAULT 'test-model',model_group text DEFAULT 'test-model',
                metadata jsonb DEFAULT '{}'::jsonb,
                "startTime" timestamp without time zone DEFAULT (now() at time zone 'UTC'));""")
        try:
            yield PostgreSQLQueries(conn)
        finally:
            await conn.execute(psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(schema)))


def scope(user: str = "user-a", key: str = "key-a", org: str = "org-a"):
    return make_scope("test-identity-secret-00000000000000000", key, "same-session", user, "team-a", org)


def admin() -> TopicViewer:
    return TopicViewer(actor_ref="test-admin", admin=True)


@pytest.mark.asyncio
async def test_concurrent_claim_and_stale_completion_cannot_cross_attempts(queries: PostgreSQLQueries) -> None:
    store: Final = SessionTopicsRepository(lambda: queries)
    identity: Final = scope()
    leases: Final = tuple(uuid.uuid4().hex for _ in range(12))
    claims: Final = await asyncio.gather(*(store.claim(identity, "test-model", lease) for lease in leases))
    assert sum(claims) == 1
    winner: Final = leases[claims.index(True)]
    await queries.execute_raw("UPDATE \"LiteLLM_SessionTopic\" SET lease_until=now()-interval '1 minute'")
    assert await store.claim(identity, "test-model", "new-lease") is True
    await store.finish(
        identity.session_ref,
        winner,
        TopicAttempt(
            state=TopicState.READY,
            result=TopicResult(
                topic="Stale result must not win",
                category="engineering",
            ),
        ),
    )
    pending: Final = await store.list_topics(admin(), 50, 0)
    assert pending.items[0].status == TopicState.PENDING
    assert pending.items[0].topic is None
    await store.finish(identity.session_ref, "new-lease", TopicAttempt(state=TopicState.INVALID_OUTPUT))
    assert await store.claim(identity, "test-model", "third-lease") is False
    assert (await store.list_topics(admin(), 50, 0)).items[0].attempts == 2


@pytest.mark.asyncio
async def test_scopes_and_owner_reads_do_not_merge_or_disclose_other_users(queries: PostgreSQLQueries) -> None:
    store: Final = SessionTopicsRepository(lambda: queries)
    first: Final = scope()
    second: Final = scope(user="user-b", key="key-b")
    await store.observe(first, "test-model", TopicState.INSUFFICIENT_INPUT)
    await store.observe(second, "test-model", TopicState.INSUFFICIENT_INPUT)
    own: Final = await store.list_topics(TopicViewer(actor_ref="viewer", user_id="user-a"), 50, 0)
    assert tuple(row.session_ref for row in own.items) == (first.session_ref,)
    assert len((await store.list_topics(admin(), 50, 0)).items) == 2
    outsider: Final = await store.list_topics(TopicViewer(actor_ref="outsider", user_id="user-c"), 50, 0)
    assert outsider.items == ()


@pytest.mark.asyncio
async def test_engine_persists_only_safe_topic_and_never_replays_ready_session(queries: PostgreSQLQueries) -> None:
    calls: Final[list[dict[str, object]]] = []  # mutable-ok: records external HTTP requests

    def transport(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "response-test",
                "object": "chat.completion",
                "created": 1,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": '{"topic":"Investigating database connection timeouts","category":"engineering"}',
                        },
                    }
                ],
            },
        )

    config: Final = TopicConfig(virtual_key=SecretStr("test-virtual-key"))
    store: Final = SessionTopicsRepository(lambda: queries)
    generator: Final = LunaTopicGenerator(config, httpx.AsyncClient(transport=httpx.MockTransport(transport)))
    engine: Final = TopicEngine(config, store, generator, clock=lambda: 100)
    text: Final = latest_user_text(
        {"messages": [{"role": "user", "content": "Investigate database timeouts api_key=raw-secret-canary"}]}
    )
    job: Final = TopicJob(scope=scope(), text=text, enqueued_at=100)
    try:
        await engine.process(job)
        await engine.process(job)
    finally:
        await generator.aclose()
    assert len(calls) == 1
    assert "raw-secret-canary" not in json.dumps(calls)
    rows: Final = await queries.query_raw('SELECT row_to_json(t) AS row FROM "LiteLLM_SessionTopic" t')
    assert "raw-secret-canary" not in json.dumps(rows, default=str)
    assert text not in json.dumps(rows, default=str)
    saved: Final = (await store.list_topics(admin(), 50, 0)).items[0]
    assert saved.topic == "Investigating database connection timeouts"
    assert saved.attempts == 1


@pytest.mark.asyncio
async def test_summary_cost_is_separate_even_when_model_and_user_are_shared(queries: PostgreSQLQueries) -> None:
    store: Final = SessionTopicsRepository(lambda: queries)
    business: Final = scope().key_hash
    worker: Final = scope(key="worker-key").key_hash
    await queries.execute_raw(
        'INSERT INTO "LiteLLM_VerificationToken" VALUES ($1,$3),($2,NULL)', business, worker, "user-a"
    )
    await queries.execute_raw(
        """INSERT INTO "LiteLLM_SpendLogs" (api_key,call_type,status,total_tokens,spend,"user")
        VALUES ($1,'acompletion','success',100,1.0,'user-a'),($2,'acompletion','success',10,0.1,'user-a'),
        ('invalid','/v1/models','failure',0,0,'')""",
        business,
        worker,
    )
    result: Final = await store.overview(admin(), (worker,), 7)
    by_kind: Final = {row.kind: row for row in result.usage}
    assert by_kind["business"].requests == 1
    assert by_kind["business"].spend == 1.0
    assert by_kind["session_topic"].requests == 1
    assert by_kind["session_topic"].spend == 0.1
    assert by_kind["other"].failed == 1
    await store.register_worker(worker)
    after_rotation: Final = await store.overview(admin(), ("f" * 64,), 7)
    assert next(row.requests for row in after_rotation.usage if row.kind == "session_topic") == 1
    own: Final = await store.overview(TopicViewer(actor_ref="user-a", user_id="user-a"), (worker,), 7)
    assert tuple(row.kind for row in own.usage) == ("business",)
    await queries.execute_raw('DELETE FROM "LiteLLM_VerificationToken" WHERE token IN ($1,$2)', business, worker)
    after_revocation: Final = await store.overview(admin(), ("f" * 64,), 7)
    assert after_revocation.usage == result.usage


@pytest.mark.asyncio
async def test_two_invalid_model_outputs_exhaust_session_budget(queries: PostgreSQLQueries) -> None:
    calls: Final[list[str]] = []  # mutable-ok: records external requests

    def transport(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(500, json={"error": {"message": "private-provider-error"}})

    config: Final = TopicConfig(virtual_key=SecretStr("test-key"))
    store: Final = SessionTopicsRepository(lambda: queries)
    generator: Final = LunaTopicGenerator(config, httpx.AsyncClient(transport=httpx.MockTransport(transport)))
    engine: Final = TopicEngine(config, store, generator, clock=lambda: 100)
    try:
        await engine.process(TopicJob(scope=scope(), text="Investigate database timeouts", enqueued_at=100))
        await engine.process(TopicJob(scope=scope(), text="Investigate database timeouts", enqueued_at=100))
    finally:
        await generator.aclose()
    assert calls == ["POST", "POST"]
    row: Final = (await store.list_topics(admin(), 50, 0)).items[0]
    assert row.attempts == 2
    assert row.status == TopicState.UPSTREAM_FAILURE
    assert row.topic is None


@pytest.mark.asyncio
async def test_audit_and_retention_do_not_store_or_retain_topic_content(queries: PostgreSQLQueries) -> None:
    store: Final = SessionTopicsRepository(lambda: queries)
    await store.observe(scope(), "test-model", TopicState.INSUFFICIENT_INPUT)
    await store.audit_read(admin(), "list", 1)
    audit: Final = await queries.query_raw(
        'SELECT actor_ref,operation,result_count FROM "LiteLLM_SessionTopicReadAudit"'
    )
    assert audit == [{"actor_ref": "test-admin", "operation": "list", "result_count": 1}]
    await queries.execute_raw("UPDATE \"LiteLLM_SessionTopic\" SET last_seen='2000-01-01'")
    await store.purge(30)
    assert (await store.list_topics(admin(), 50, 0)).items == ()


@pytest.mark.asyncio
async def test_request_hook_ignores_spoofed_purpose_but_skips_authenticated_summary_key(
    queries: PostgreSQLQueries,
) -> None:
    calls: Final[list[str]] = []  # mutable-ok: records external model requests

    def transport(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(
            200,
            json={
                "id": "reply",
                "object": "chat.completion",
                "created": 1,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": '{"topic":"Investigating database timeouts","category":"engineering"}',
                        },
                    }
                ],
            },
        )

    config: Final = TopicConfig(
        enabled=True,
        virtual_key=SecretStr("worker-test-key"),
        identity_secret=SecretStr("test-identity-secret-00000000000000000"),
        allowed_key_hashes=frozenset({"*"}),
    )
    generator: Final = LunaTopicGenerator(config, httpx.AsyncClient(transport=httpx.MockTransport(transport)))
    runtime: Final = SessionTopicRuntime(config, SessionTopicsRepository(lambda: queries), generator)
    runtime.engine.start()
    business_auth: Final = UserAPIKeyAuth(api_key=scope().key_hash, user_id="user-a")
    worker_auth: Final = UserAPIKeyAuth(api_key=next(iter(runtime.hook.worker_hashes)))
    body: Final = {
        "litellm_session_id": "session-123",
        "metadata": {"purpose": "session_topic", "skip_summary": True},
        "messages": [{"role": "user", "content": "Investigate database timeouts"}],
    }
    try:
        assert await runtime.hook.async_pre_call_hook(business_auth, DualCache(), body, "completion") is not None
        await asyncio.wait_for(runtime.engine.queue.join(), timeout=10)
        internal: Final = await runtime.hook.async_pre_call_hook(worker_auth, DualCache(), body, "completion")
        await asyncio.wait_for(runtime.engine.queue.join(), timeout=10)
        assert calls == ["POST"]
        assert internal is not None
        assert internal["store"] is False
        assert internal["num_retries"] == 0
        assert internal["disable_fallbacks"] is True
        assert len((await runtime.store.list_topics(admin(), 50, 0)).items) == 1
    finally:
        await runtime.engine.stop()
        await generator.aclose()


@pytest.mark.asyncio
async def test_real_reader_routes_filter_users_and_deny_summary_identity(queries: PostgreSQLQueries) -> None:
    store: Final = SessionTopicsRepository(lambda: queries)
    await store.observe(scope(), "test-model", TopicState.INSUFFICIENT_INPUT)
    await store.observe(scope(user="user-b", key="key-b"), "test-model", TopicState.INSUFFICIENT_INPUT)
    config: Final = TopicConfig(virtual_key=SecretStr("test-reader-worker"), identity_secret=SecretStr("identity-test"))
    generator: Final = LunaTopicGenerator(
        config,
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(500),
            )
        ),
    )
    runtime: Final = SessionTopicRuntime(config, store, generator)
    app: Final = FastAPI()
    app.include_router(topics_router)
    app.state.session_topics = runtime
    app.dependency_overrides[get_session_topics_repository] = lambda: store
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            app.dependency_overrides[user_api_key_auth] = lambda: UserAPIKeyAuth(
                user_id="user-a",
                user_role="internal_user",
                team_id=UI_SESSION_TOKEN_TEAM_ID,
            )
            own: Final = await client.get("/session_topics")
            assert own.status_code == 200
            assert [row["user_id"] for row in own.json()["items"]] == ["user-a"]
            await store.observe(scope(user="user-a", key="sibling-key"), "test-model", TopicState.INSUFFICIENT_INPUT)
            app.dependency_overrides[user_api_key_auth] = lambda: UserAPIKeyAuth(
                user_id="user-a",
                user_role="internal_user",
                api_key=scope().key_hash,
            )
            key_scoped: Final = await client.get("/session_topics")
            assert key_scoped.status_code == 200
            assert [row["key_hash"] for row in key_scoped.json()["items"]] == [scope().key_hash]
            costs: Final = await client.get("/session_topics/costs")
            assert costs.status_code == 200, costs.text
            assert [row["key_hash"] for row in costs.json()["items"]] == [scope().key_hash]
            assert costs.json()["items"][0]["summary"] is None
            assert (await client.get("/session_topics/costs", params={"group_by": "invalid"})).status_code == 422
            app.dependency_overrides[user_api_key_auth] = lambda: UserAPIKeyAuth(
                user_id="admin", user_role="proxy_admin"
            )
            all_rows: Final = await client.get("/session_topics")
            assert all_rows.status_code == 200
            assert len(all_rows.json()["items"]) == 3
            app.dependency_overrides[user_api_key_auth] = lambda: UserAPIKeyAuth(
                api_key=next(iter(runtime.hook.worker_hashes)),
                user_role="proxy_admin",
            )
            assert (await client.get("/session_topics")).status_code == 403
            assert (await client.get("/session_topics/costs")).status_code == 403
    finally:
        app.dependency_overrides.clear()
        await generator.aclose()


@pytest.mark.asyncio
async def test_authenticated_organization_separates_identical_key_and_session(queries: PostgreSQLQueries) -> None:
    config: Final = cost_config().model_copy(update={"enabled": True, "allowed_key_hashes": frozenset({"*"})})
    generator: Final = LunaTopicGenerator(
        config, httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    )
    runtime: Final = SessionTopicRuntime(config, SessionTopicsRepository(lambda: queries), generator)
    environment: Final = {
        "LITELLM_SESSION_TOPICS_ENABLED": "true",
        "LITELLM_SESSION_TOPICS_IDENTITY_SECRET": config.identity_secret.get_secret_value(),
    }
    runtime.engine.start()
    try:
        for org in ("org-a", "org-b"):
            identity: Final = scope(org=org)
            auth: Final = UserAPIKeyAuth(
                api_key=identity.key_hash, user_id="user-a", team_id="team-a", organization_id=org
            )
            stamped: Final = await runtime.hook.async_pre_call_hook(
                auth, DualCache(), {"litellm_session_id": "same-session", "messages": []}, "completion"
            )
            assert stamped is not None
            projected: Final = topic_cost_log_metadata(
                stamped.get("metadata"), identity.key_hash, None, environment=environment
            )
            assert projected is not None and projected["session_topic"] == {
                "version": 1,
                "session_ref": identity.session_ref,
                "kind": "business",
            }
        await asyncio.wait_for(runtime.engine.queue.join(), timeout=10)
        records: Final = (await runtime.store.list_topics(admin(), 50, 0)).items
        assert {record.organization_id for record in records} == {"org-a", "org-b"}
        assert len({record.session_ref for record in records}) == 2
    finally:
        await runtime.engine.stop()
        await generator.aclose()


def cost_config() -> TopicConfig:
    return TopicConfig(
        identity_secret=SecretStr("test-identity-secret-00000000000000000"),
        virtual_key=SecretStr("cost-worker"),
    )


async def ready_topic(store: SessionTopicsRepository, identity: TopicScope) -> None:
    assert await store.claim(identity, "test-model", "cost-test-lease")
    await store.finish(
        identity.session_ref,
        "cost-test-lease",
        TopicAttempt(
            state=TopicState.READY, result=TopicResult(topic="Reviewing database connections", category="engineering")
        ),
    )


async def record_cost(
    queries: PostgreSQLQueries,
    request_id: str,
    identity: TopicScope,
    amount: float,
    *,
    kind: str = "business",
    price: str | None = "known",
    linked: bool = True,
    legacy: bool = False,
    status: str = "success",
) -> None:
    key: Final = scope(key="cost-worker").key_hash if kind == "session_topic" else identity.key_hash
    attribution: Final = {"version": 1, "session_ref": identity.session_ref, "kind": kind} if linked else None
    metadata: Final = {
        **({} if legacy else {"session_topic": attribution}),
        **({"session_topic_cost_status": price} if price is not None else {}),
    }
    await queries.execute_raw(
        """INSERT INTO "LiteLLM_SpendLogs"
        (request_id,api_key,call_type,status,total_tokens,spend,"user",team_id,organization_id,session_id,metadata)
        VALUES($1,$2,'acompletion',$3,10,$4,$5,$6,$7,'same-session',$8::jsonb)
        ON CONFLICT(request_id) DO NOTHING""",
        request_id,
        key,
        status,
        amount,
        identity.user_id,
        identity.team_id,
        identity.organization_id,
        json.dumps(metadata),
    )


@pytest.mark.asyncio
async def test_session_costs_preserve_retries_unpriced_calls_and_unassociated_spend(queries: PostgreSQLQueries) -> None:
    store: Final = SessionTopicsRepository(lambda: queries)
    first: Final = scope()
    second: Final = scope(key="sibling-key")
    await ready_topic(store, first)
    await ready_topic(store, second)
    await record_cost(queries, "business-one", first, 1)
    await record_cost(queries, "business-one", first, 1)
    await record_cost(queries, "business-two", second, 2)
    await record_cost(queries, "unpriced", first, 0, price="unpriced")
    await record_cost(queries, "summary-one", first, 0.1, kind="session_topic")
    await record_cost(queries, "summary-retry", first, 0.02, kind="session_topic", status="failure")
    await record_cost(queries, "legacy-summary", first, 0.03, kind="session_topic", legacy=True, linked=False)
    await record_cost(queries, "no-session", first, 0.5, linked=False)
    page: Final = await store.costs(admin(), cost_config(), TopicCostQuery(group_by="topic"))
    assert page.total_groups == 1 and len(page.items) == 1
    item: Final = page.items[0]
    assert item.sessions == 2 and item.ready_sessions == 2
    assert item.business.requests == 3 and item.business.recorded_spend == 3
    assert item.business.amount is None and item.business.unpriced_requests == 1
    assert item.summary is not None and item.summary.requests == 2 and item.summary.failed == 1
    assert item.summary.amount == pytest.approx(0.12)
    assert item.total is not None and item.total.amount is None
    assert item.total.recorded_spend == pytest.approx(3.12)
    unassociated: Final = {bucket.kind: bucket.costs for bucket in page.unassociated}
    assert unassociated["business"].recorded_spend == 0.5
    assert unassociated["session_topic"].recorded_spend == 0.03
    assert sum(bucket.costs.recorded_spend for bucket in page.totals) == pytest.approx(3.65)
    own: Final = await store.costs(
        TopicViewer(actor_ref="first-key", key_hash=first.key_hash), cost_config(), TopicCostQuery()
    )
    assert len(own.items) == 1 and own.items[0].key_hash == first.key_hash
    assert own.items[0].summary is None and own.items[0].total is None
    assert tuple(bucket.kind for bucket in own.totals) == ("business",)
    owner: Final = await store.costs(TopicViewer(actor_ref="owner", user_id="user-a"), cost_config(), TopicCostQuery())
    assert len(owner.items) == 2
    assert tuple(bucket.kind for bucket in owner.totals) == ("business",)


@pytest.mark.asyncio
async def test_legacy_business_recovery_does_not_guess_summary_links_or_reprice_old_zeroes(
    queries: PostgreSQLQueries,
) -> None:
    store: Final = SessionTopicsRepository(lambda: queries)
    identity: Final = scope()
    await ready_topic(store, identity)
    await record_cost(queries, "historical-known", identity, 0.6, legacy=True, linked=False, price=None)
    await record_cost(queries, "historical-unknown", identity, 0, legacy=True, linked=False, price=None)
    await record_cost(queries, "historical-summary", identity, 0.07, kind="session_topic", legacy=True, price=None)
    await record_cost(queries, "known-free", identity, 0)
    page: Final = await store.costs(admin(), cost_config(), TopicCostQuery())
    assert len(page.items) == 1
    item: Final = page.items[0]
    assert item.historical_business_requests == 2
    assert item.business.requests == 3 and item.business.recorded_spend == 0.6
    assert item.business.amount is None and item.business.unknown_cost_requests == 1
    assert item.summary is not None and item.summary.requests == 0
    assert page.unassociated[0].kind == "session_topic" and page.unassociated[0].costs.recorded_spend == 0.07
    wrong_secret: Final = cost_config().model_copy(update={"identity_secret": SecretStr("different-identity-secret")})
    unreadable: Final = await store.costs(admin(), wrong_secret, TopicCostQuery())
    assert unreadable.items[0].historical_business_requests == 0
    assert unreadable.items[0].business.requests == 1 and unreadable.items[0].business.amount == 0


@pytest.mark.asyncio
async def test_cost_queries_cannot_attribute_a_business_charge_to_a_sibling_key(queries: PostgreSQLQueries) -> None:
    store: Final = SessionTopicsRepository(lambda: queries)
    first: Final = scope()
    second: Final = scope(user="other-user", key="other-key")
    await ready_topic(store, first)
    await ready_topic(store, second)
    await record_cost(queries, "forged-import", first, 7)
    await queries.execute_raw(
        """UPDATE "LiteLLM_SpendLogs" SET metadata=jsonb_set(metadata,'{session_topic,session_ref}',to_jsonb($1::text))
           WHERE request_id='forged-import'""",
        second.session_ref,
    )
    page: Final = await store.costs(admin(), cost_config(), TopicCostQuery())
    assert all(item.business.requests == 0 for item in page.items)
    assert page.unassociated[0].costs.recorded_spend == 7
    foreign: Final = await store.costs(
        TopicViewer(actor_ref="other", key_hash=second.key_hash), cost_config(), TopicCostQuery()
    )
    assert foreign.totals == () and foreign.unassociated == ()


@pytest.mark.asyncio
async def test_legacy_scan_reports_its_bound_without_dropping_unmatched_charges(queries: PostgreSQLQueries) -> None:
    store: Final = SessionTopicsRepository(lambda: queries)
    identity: Final = scope()
    other: Final = scope(key="older-key")
    await ready_topic(store, identity)
    await ready_topic(store, other)
    await record_cost(queries, "older-charge", other, 4, legacy=True, linked=False, price=None)
    await queries.execute_raw("""UPDATE "LiteLLM_SpendLogs" SET "startTime"=now()-interval '1 hour'""")
    await queries.execute_raw(
        """INSERT INTO "LiteLLM_SpendLogs"
        (request_id,api_key,call_type,status,total_tokens,spend,"user",team_id,organization_id,session_id)
        SELECT 'recent-'||n,$1,'acompletion','success',1,0.01,'user-a','team-a','org-a','same-session'
        FROM generate_series(1,1001) n""",
        identity.key_hash,
    )
    page: Final = await store.costs(admin(), cost_config(), TopicCostQuery())
    assert page.legacy_scan_limit == 1000 and page.legacy_scan_truncated is True
    assert sum(item.business.requests for item in page.items) == 1001
    assert page.unassociated[0].costs.requests == 1 and page.unassociated[0].costs.recorded_spend == 4


@pytest.mark.asyncio
async def test_cost_pagination_keeps_window_totals_and_excludes_future_charges(queries: PostgreSQLQueries) -> None:
    store: Final = SessionTopicsRepository(lambda: queries)
    first: Final = scope()
    second: Final = scope(key="second-key")
    await ready_topic(store, first)
    await ready_topic(store, second)
    await record_cost(queries, "recent", first, 1)
    await record_cost(queries, "older", second, 3)
    await record_cost(queries, "future", first, 100)
    await queries.execute_raw(
        """UPDATE "LiteLLM_SpendLogs" SET "startTime"=now()-interval '10 days' WHERE request_id='older'"""
    )
    await queries.execute_raw(
        """UPDATE "LiteLLM_SpendLogs" SET "startTime"=now()+interval '1 day' WHERE request_id='future'"""
    )
    narrow: Final = await store.costs(admin(), cost_config(), TopicCostQuery(days=7))
    assert narrow.totals[0].costs.recorded_spend == 1
    full: Final = await store.costs(admin(), cost_config(), TopicCostQuery(days=30, group_by="key", limit=1))
    next_page: Final = await store.costs(
        admin(), cost_config(), TopicCostQuery(days=30, group_by="key", limit=1, offset=1)
    )
    assert full.total_groups == next_page.total_groups == 2
    assert full.items[0].key_hash == second.key_hash and next_page.items[0].key_hash == first.key_hash
    assert full.totals == next_page.totals and full.totals[0].costs.recorded_spend == 4

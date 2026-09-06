from __future__ import annotations

import os
import threading
import time
from datetime import UTC, datetime, timedelta
from queue import Queue
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text, update
from sqlalchemy.orm import sessionmaker

from db import billing_repository as repository
from server.billing.errors import UsageAllowanceExceededError
from server.billing.metering_service import (
    expire_stale_reservations,
    reserve_usage,
    settle_usage,
    settle_usage_with_supplement,
)
from server.billing.plan_catalog import get_plan_catalog
from server.billing.subscription_service import EffectiveSubscription

POSTGRES_URL = os.getenv("BILLING_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not POSTGRES_URL,
        reason="BILLING_TEST_DATABASE_URL is required for PostgreSQL billing integration tests",
    ),
]


@pytest.fixture()
def postgres_runtime(monkeypatch):
    assert POSTGRES_URL is not None
    monkeypatch.setenv("DATABASE_URL", POSTGRES_URL)
    monkeypatch.setenv("DB_SCHEMA", "public")

    import db.engine as db_engine
    import db.tables as db_tables

    if db_engine._ENGINE is not None:
        db_engine._ENGINE.dispose()
    db_engine._ENGINE = None
    db_tables.DB_SCHEMA = "public"
    db_tables._tables_cache.clear()
    db_tables.metadata.clear()

    engine = create_engine(POSTGRES_URL)
    try:
        yield engine
    finally:
        engine.dispose()
        if db_engine._ENGINE is not None:
            db_engine._ENGINE.dispose()
        db_engine._ENGINE = None
        db_tables._tables_cache.clear()
        db_tables.metadata.clear()


def _delete_test_account(engine, account_id) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM credit_transactions WHERE billing_account_id = :account_id"),
            {"account_id": account_id},
        )
        connection.execute(
            text("DELETE FROM usage_reservations WHERE billing_account_id = :account_id"),
            {"account_id": account_id},
        )
        connection.execute(
            text(
                "DELETE FROM usage_counters WHERE usage_period_id IN "
                "(SELECT id FROM usage_periods WHERE billing_account_id = :account_id)"
            ),
            {"account_id": account_id},
        )
        connection.execute(
            text("DELETE FROM usage_periods WHERE billing_account_id = :account_id"),
            {"account_id": account_id},
        )
        connection.execute(
            text("DELETE FROM subscription_grants WHERE billing_account_id = :account_id"),
            {"account_id": account_id},
        )
        connection.execute(
            text("DELETE FROM subscriptions WHERE billing_account_id = :account_id"),
            {"account_id": account_id},
        )
        connection.execute(
            text("DELETE FROM billing_accounts WHERE id = :account_id"),
            {"account_id": account_id},
        )


def test_billing_schema_is_queryable_through_reflection(postgres_runtime):
    from db.tables import get_table

    for table_name in (
        "billing_accounts",
        "subscriptions",
        "subscription_grants",
        "usage_periods",
        "usage_counters",
        "usage_reservations",
        "credit_transactions",
        "billing_webhook_events",
        "cortex_analysis_runs",
    ):
        table = get_table(table_name)
        assert table.name == table_name
        assert table.schema == "public"
    assert "last_activity_at" in get_table("usage_reservations").c
    assert "subscription_grant_id" in get_table("usage_periods").c


def test_concurrent_grant_issuance_serializes_on_account(postgres_runtime):
    session_factory = sessionmaker(bind=postgres_runtime)
    with session_factory.begin() as db:
        account = repository.get_or_create_billing_account_for_user(db, uuid4())
    now = datetime.now(UTC)
    first_inserted = threading.Event()
    release_first = threading.Event()
    second_finished = threading.Event()
    failures: Queue[BaseException] = Queue()
    results: Queue[str] = Queue()

    def issue(db):
        repository.create_subscription_grant(
            db,
            billing_account_id=account["id"],
            plan_code="pro",
            starts_at=now,
            expires_at=now + timedelta(days=90),
            granted_by="test_operator",
            reason="concurrency_test",
            now=now,
        )

    def first_transaction():
        try:
            with session_factory.begin() as db:
                issue(db)
                first_inserted.set()
                if not release_first.wait(5):
                    raise TimeoutError("First grant lock was not released")
        except BaseException as exc:
            failures.put(exc)

    def second_transaction():
        try:
            if not first_inserted.wait(5):
                raise TimeoutError("First grant was not inserted")
            with session_factory.begin() as db:
                issue(db)
            results.put("unexpected_duplicate")
        except ValueError as exc:
            results.put(str(exc))
        except BaseException as exc:
            failures.put(exc)
        finally:
            second_finished.set()

    first = threading.Thread(target=first_transaction)
    second = threading.Thread(target=second_transaction)
    try:
        first.start()
        assert first_inserted.wait(5)
        second.start()
        assert not second_finished.wait(0.25)
        release_first.set()
        first.join(5)
        second.join(5)
        assert second_finished.is_set()
        assert failures.empty(), list(failures.queue)
        assert "open grant already exists" in results.get_nowait()
        with session_factory() as db:
            grant = repository.get_effective_subscription_grant(db, account["id"], now)
            assert grant["plan_code"] == "pro"
    finally:
        release_first.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
        _delete_test_account(postgres_runtime, account["id"])


def test_lock_usage_counters_serializes_concurrent_reservation_paths(postgres_runtime):
    session_factory = sessionmaker(bind=postgres_runtime)
    setup_session = session_factory()
    account = repository.get_or_create_billing_account_for_user(setup_session, uuid4())
    starts_at = datetime(2026, 7, 1, tzinfo=UTC)
    period = repository.create_usage_period(
        setup_session,
        billing_account_id=account["id"],
        plan_code="free",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(days=31),
    )
    repository.get_or_create_usage_counter(
        setup_session,
        period["id"],
        "ai_credits",
    )
    setup_session.commit()
    setup_session.close()

    first_has_lock = threading.Event()
    release_first = threading.Event()
    second_has_lock = threading.Event()
    failures: Queue[BaseException] = Queue()

    def first_transaction() -> None:
        session = session_factory()
        try:
            repository.lock_usage_counters(
                session,
                period["id"],
                ["ai_credits"],
            )
            first_has_lock.set()
            if not release_first.wait(timeout=5):
                raise TimeoutError("Timed out waiting to release the first counter lock")
            session.commit()
        except BaseException as exc:
            session.rollback()
            failures.put(exc)
        finally:
            session.close()

    def second_transaction() -> None:
        session = session_factory()
        try:
            if not first_has_lock.wait(timeout=5):
                raise TimeoutError("First transaction never acquired the counter lock")
            repository.lock_usage_counters(
                session,
                period["id"],
                ["ai_credits"],
            )
            second_has_lock.set()
            session.commit()
        except BaseException as exc:
            session.rollback()
            failures.put(exc)
        finally:
            session.close()

    first_thread = threading.Thread(target=first_transaction)
    second_thread = threading.Thread(target=second_transaction)
    try:
        first_thread.start()
        assert first_has_lock.wait(timeout=5)
        second_thread.start()
        time.sleep(0.25)
        assert not second_has_lock.is_set()
        release_first.set()
        first_thread.join(timeout=5)
        second_thread.join(timeout=5)
        assert second_has_lock.is_set()
        assert failures.empty(), list(failures.queue)
    finally:
        release_first.set()
        first_thread.join(timeout=5)
        second_thread.join(timeout=5)
        _delete_test_account(postgres_runtime, account["id"])


def test_concurrent_metering_prevents_overuse_and_settles_the_winner(postgres_runtime):
    from db.tables import get_table

    session_factory = sessionmaker(bind=postgres_runtime)
    setup_session = session_factory()
    account = repository.get_or_create_billing_account_for_user(setup_session, uuid4())
    starts_at = datetime(2026, 7, 1, tzinfo=UTC)
    period = repository.create_usage_period(
        setup_session,
        billing_account_id=account["id"],
        plan_code="free",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(days=31),
    )
    counter = repository.get_or_create_usage_counter(
        setup_session,
        period["id"],
        "ai_credits",
    )
    usage_counters = get_table("usage_counters")
    setup_session.execute(
        update(usage_counters)
        .where(usage_counters.c.id == counter["id"])
        .values(used_quantity=get_plan_catalog().require("free").allowances.ai_credits - 1)
    )
    setup_session.commit()
    setup_session.close()

    effective = EffectiveSubscription(
        billing_account_id=account["id"],
        usage_period_id=period["id"],
        plan=get_plan_catalog().require("free"),
        source="test",
        provider=None,
        provider_subscription_id=None,
        status="free",
        current_period_start=starts_at,
        current_period_end=starts_at + timedelta(days=31),
        cancel_at_period_end=False,
        grace_until=None,
    )
    start = threading.Barrier(2)
    outcomes: Queue[tuple[str, object]] = Queue()

    def reserve(request_id: str) -> None:
        session = session_factory()
        try:
            start.wait(timeout=5)
            reservation = reserve_usage(
                session,
                effective_subscription=effective,
                request_id=request_id,
                operation_type="ask",
                requested_quantities={"ai_credits": 1},
            )
            session.commit()
            outcomes.put(("reserved", reservation.id))
        except UsageAllowanceExceededError as exc:
            session.rollback()
            outcomes.put(("denied", exc))
        except BaseException as exc:
            session.rollback()
            outcomes.put(("failed", exc))
        finally:
            session.close()

    threads = [
        threading.Thread(target=reserve, args=("req-concurrent-1",)),
        threading.Thread(target=reserve, args=("req-concurrent-2",)),
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert all(not thread.is_alive() for thread in threads)

        results = [outcomes.get_nowait(), outcomes.get_nowait()]
        assert sorted(result[0] for result in results) == ["denied", "reserved"]
        reservation_id = next(value for state, value in results if state == "reserved")

        verification = session_factory()
        try:
            before_settlement = repository.get_or_create_usage_counter(
                verification,
                period["id"],
                "ai_credits",
            )
            allowance_limit = get_plan_catalog().require("free").allowances.ai_credits
            assert before_settlement["used_quantity"] == allowance_limit - 1
            assert before_settlement["reserved_quantity"] == 1

            settle_usage(
                verification,
                reservation_id=reservation_id,
                successful_quantities={"ai_credits": 1},
            )
            verification.commit()
            after_settlement = repository.get_or_create_usage_counter(
                verification,
                period["id"],
                "ai_credits",
            )
            assert after_settlement["used_quantity"] == allowance_limit
            assert after_settlement["reserved_quantity"] == 0
        finally:
            verification.close()
    finally:
        for thread in threads:
            thread.join(timeout=5)
        _delete_test_account(postgres_runtime, account["id"])


def test_concurrent_supplemental_settlement_allocates_remaining_credits_once(
    postgres_runtime,
):
    from db.tables import get_table

    session_factory = sessionmaker(bind=postgres_runtime)
    setup_session = session_factory()
    account = repository.get_or_create_billing_account_for_user(setup_session, uuid4())
    starts_at = datetime(2026, 7, 1, tzinfo=UTC)
    period = repository.create_usage_period(
        setup_session,
        billing_account_id=account["id"],
        plan_code="free",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(days=31),
    )
    counter = repository.get_or_create_usage_counter(
        setup_session,
        period["id"],
        "ai_credits",
    )
    allowance_limit = get_plan_catalog().require("free").allowances.ai_credits
    usage_counters = get_table("usage_counters")
    setup_session.execute(
        update(usage_counters)
        .where(usage_counters.c.id == counter["id"])
        .values(used_quantity=allowance_limit - 350)
    )
    effective = EffectiveSubscription(
        billing_account_id=account["id"],
        usage_period_id=period["id"],
        plan=get_plan_catalog().require("free"),
        source="test",
        provider=None,
        provider_subscription_id=None,
        status="free",
        current_period_start=starts_at,
        current_period_end=starts_at + timedelta(days=31),
        cancel_at_period_end=False,
        grace_until=None,
    )
    reservations = [
        reserve_usage(
            setup_session,
            effective_subscription=effective,
            request_id=f"supplement-concurrent-{index}",
            operation_type="ask",
            requested_quantities={"ai_credits": 100},
        )
        for index in range(2)
    ]
    setup_session.commit()
    setup_session.close()

    start = threading.Barrier(2)
    outcomes: Queue[tuple[int, int]] = Queue()
    failures: Queue[BaseException] = Queue()

    def settle(reservation_id) -> None:
        session = session_factory()
        try:
            start.wait(timeout=5)
            outcome = settle_usage_with_supplement(
                session,
                reservation_id=reservation_id,
                actual_quantity=200,
                allowance_limit=allowance_limit,
            )
            session.commit()
            outcomes.put((outcome.billed_quantity, outcome.supplemented_quantity))
        except BaseException as exc:
            session.rollback()
            failures.put(exc)
        finally:
            session.close()

    threads = [
        threading.Thread(target=settle, args=(reservation.id,)) for reservation in reservations
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert all(not thread.is_alive() for thread in threads)
        assert failures.empty(), list(failures.queue)
        assert sorted(outcomes.get_nowait() for _ in range(2)) == [(100, 0), (200, 100)]

        verification = session_factory()
        try:
            final_counter = repository.get_or_create_usage_counter(
                verification,
                period["id"],
                "ai_credits",
            )
            assert final_counter["used_quantity"] == allowance_limit - 50
            assert final_counter["reserved_quantity"] == 0
            assert final_counter["used_quantity"] <= allowance_limit
        finally:
            verification.close()
    finally:
        for thread in threads:
            thread.join(timeout=5)
        _delete_test_account(postgres_runtime, account["id"])


def test_concurrent_cleanup_workers_release_one_stale_reservation_once(
    postgres_runtime,
):
    from db.tables import get_table

    session_factory = sessionmaker(bind=postgres_runtime)
    setup_session = session_factory()
    account = repository.get_or_create_billing_account_for_user(setup_session, uuid4())
    starts_at = datetime(2026, 7, 1, tzinfo=UTC)
    period = repository.create_usage_period(
        setup_session,
        billing_account_id=account["id"],
        plan_code="free",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(days=31),
    )
    effective = EffectiveSubscription(
        billing_account_id=account["id"],
        usage_period_id=period["id"],
        plan=get_plan_catalog().require("free"),
        source="test",
        provider=None,
        provider_subscription_id=None,
        status="free",
        current_period_start=starts_at,
        current_period_end=starts_at + timedelta(days=31),
        cancel_at_period_end=False,
        grace_until=None,
    )
    reservation = reserve_usage(
        setup_session,
        effective_subscription=effective,
        request_id="cleanup-concurrent",
        operation_type="ask",
        requested_quantities={"ai_credits": 500},
    )
    usage_reservations = get_table("usage_reservations")
    setup_session.execute(
        update(usage_reservations)
        .where(usage_reservations.c.id == reservation.id)
        .values(last_activity_at=datetime.now(UTC) - timedelta(hours=1))
    )
    setup_session.commit()
    setup_session.close()

    start = threading.Barrier(2)
    outcomes: Queue[tuple[int, int]] = Queue()
    failures: Queue[BaseException] = Queue()

    def cleanup() -> None:
        session = session_factory()
        try:
            start.wait(timeout=5)
            outcome = expire_stale_reservations(
                session,
                older_than=datetime.now(UTC) - timedelta(minutes=30),
            )
            session.commit()
            outcomes.put((outcome.released, outcome.credits_released))
        except BaseException as exc:
            session.rollback()
            failures.put(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=cleanup) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert all(not thread.is_alive() for thread in threads)
        assert failures.empty(), list(failures.queue)
        cleanup_results = [outcomes.get_nowait() for _ in range(2)]
        assert sum(item[0] for item in cleanup_results) == 1
        assert sum(item[1] for item in cleanup_results) == 500

        verification = session_factory()
        try:
            persisted = repository.get_usage_reservation_by_id(
                verification,
                reservation.id,
            )
            counter = repository.get_or_create_usage_counter(
                verification,
                period["id"],
                "ai_credits",
            )
            assert persisted["state"] == "released"
            assert persisted["release_reason"] == "stale_reservation_expired"
            assert counter["reserved_quantity"] == 0
            assert counter["used_quantity"] == 0
        finally:
            verification.close()
    finally:
        for thread in threads:
            thread.join(timeout=5)
        _delete_test_account(postgres_runtime, account["id"])

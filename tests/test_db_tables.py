from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

from db import tables


def test_get_table_serializes_concurrent_first_reflection(monkeypatch):
    table_name = "work_oauth_states"
    previous = tables._tables_cache.pop(table_name, None)
    first_reflection_started = Event()
    second_call_started = Event()
    release_first_reflection = Event()
    calls_guard = Lock()
    reflection_calls = 0
    reflected_table = object()

    def controlled_reflection(name: str):
        nonlocal reflection_calls
        assert name == table_name
        with calls_guard:
            reflection_calls += 1
        first_reflection_started.set()
        assert release_first_reflection.wait(timeout=2)
        return reflected_table

    def second_lookup():
        second_call_started.set()
        return tables.get_table(table_name)

    monkeypatch.setattr(tables, "reflect_table", controlled_reflection)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(tables.get_table, table_name)
            assert first_reflection_started.wait(timeout=2)
            second = executor.submit(second_lookup)
            assert second_call_started.wait(timeout=2)
            release_first_reflection.set()

            assert first.result(timeout=2) is reflected_table
            assert second.result(timeout=2) is reflected_table
            assert reflection_calls == 1
    finally:
        release_first_reflection.set()
        tables._tables_cache.pop(table_name, None)
        if previous is not None:
            tables._tables_cache[table_name] = previous

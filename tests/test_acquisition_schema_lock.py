import asyncio

from valuechain import acquisition_schema
from valuechain.acquisition_schema import AcquisitionSchemaGuard, SCHEMA_LOCK_ID


def test_schema_lock_id_is_stable_and_positive() -> None:
    assert SCHEMA_LOCK_ID == 8_641_969


def test_async_schema_guard_prepares_only_once(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        acquisition_schema,
        "prepare_acquisition_schema",
        lambda database_url: calls.append(database_url),
    )
    guard = AcquisitionSchemaGuard("postgresql://test")

    async def run() -> None:
        await guard.prepare()
        await guard.prepare()

    asyncio.run(run())

    assert calls == ["postgresql://test"]
    assert guard.prepared

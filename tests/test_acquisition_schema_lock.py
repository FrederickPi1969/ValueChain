from valuechain.acquisition_schema import SCHEMA_LOCK_ID


def test_schema_lock_id_is_stable_and_positive() -> None:
    assert SCHEMA_LOCK_ID == 8_641_969

import pytest

from gcu.config import Settings


def test_http_retries_default_to_five() -> None:
    assert Settings(_env_file=None).http_max_retries == 5


def test_http_retries_are_capped_at_five() -> None:
    assert Settings(_env_file=None, http_max_retries=99).http_max_retries == 5


def test_http_retries_cannot_be_negative() -> None:
    with pytest.raises(ValueError, match="must be non-negative"):
        Settings(_env_file=None, http_max_retries=-1)

import pytest
import requests

from valuechain.proxy_pool import ProxyPoolClient, ProxyPoolError, parse_normal_proxy


def test_parse_normal_proxy_masks_credentials_and_encodes_url() -> None:
    proxy = parse_normal_proxy("proxy.example:8080:user@example:p/a:ss")

    assert proxy.masked == "proxy.example:8080"
    assert proxy.url == "http://user%40example:p%2Fa%3Ass@proxy.example:8080"


@pytest.mark.parametrize(
    "value",
    ["", "host:80", "host:not-a-port:user:password", "host:70000:user:password"],
)
def test_parse_normal_proxy_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ProxyPoolError):
        parse_normal_proxy(value)


def test_proxy_pool_retries_transient_control_plane_failure(monkeypatch) -> None:
    class Response:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise requests.HTTPError(f"status={self.status_code}")

        def json(self) -> dict[str, str]:
            return {"proxy": "proxy.example:8080:user:password"}

    class Session:
        def __init__(self) -> None:
            self.responses = [Response(502), Response(200)]
            self.calls = 0

        def get(self, *_args, **_kwargs) -> Response:
            self.calls += 1
            return self.responses.pop(0)

    client = ProxyPoolClient("https://proxy.example", backoff_seconds=0)
    session = Session()
    client.session = session

    endpoint = client.random_normal()

    assert endpoint.masked == "proxy.example:8080"
    assert session.calls == 2


def test_proxy_pool_stops_after_five_retries() -> None:
    class Session:
        calls = 0

        def get(self, *_args, **_kwargs):
            self.calls += 1
            raise requests.ConnectionError("temporary failure")

    client = ProxyPoolClient(
        "https://proxy.example",
        max_retries=99,
        backoff_seconds=0,
    )
    session = Session()
    client.session = session

    with pytest.raises(ProxyPoolError, match="after retries"):
        client.random_normal()

    assert session.calls == 6

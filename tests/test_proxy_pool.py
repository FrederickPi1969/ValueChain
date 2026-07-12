import pytest

from valuechain.proxy_pool import ProxyPoolError, parse_normal_proxy


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

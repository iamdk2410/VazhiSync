from app.healthcheck import check_http, main


def test_rejects_non_http_scheme() -> None:
    assert check_http("file:///etc/passwd") is False


def test_unreachable_url_is_unhealthy() -> None:
    assert check_http("http://127.0.0.1:1/health") is False


def test_bad_arguments_return_usage_code() -> None:
    assert main([]) == 2
    assert main(["unknown"]) == 2

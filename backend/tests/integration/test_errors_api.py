"""Every error uses the same shape, carries the request id, and leaks nothing internal."""

import pytest
from sqlalchemy.exc import OperationalError


def _assert_error_shape(response, status: int, code: str) -> dict:
    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert set(body) == {"error"}
    error = body["error"]
    assert set(error) == {"code", "message", "details", "request_id"}
    assert error["code"] == code
    assert error["message"]
    assert isinstance(error["details"], dict)
    # The id in the body is the one in the X-Request-ID header and in the server logs.
    assert error["request_id"] == response.headers["X-Request-ID"]
    return error


def test_unknown_route_is_404(api):
    _assert_error_shape(api.get("/v1/nope"), 404, "not_found")


def test_wrong_method_is_405(api):
    _assert_error_shape(api.delete("/v1/jobs"), 405, "method_not_allowed")


def test_validation_error_never_echoes_the_submitted_value(api):
    response = api.get("/v1/jobs", params={"limit": "SECRET-VALUE-123"})
    error = _assert_error_shape(response, 422, "invalid_request")
    assert error["details"]["fields"] == [
        {"location": "query", "field": "limit", "issue": error["details"]["fields"][0]["issue"]}
    ]
    assert "SECRET-VALUE-123" not in response.text


def test_service_errors_use_the_shape(api):
    _assert_error_shape(api.get("/v1/jobs/not-a-uuid"), 404, "job_not_found")


def test_unexpected_exception_is_a_generic_500(api):
    def boom():
        raise RuntimeError("secret internal detail: password=hunter2")

    api.app.add_api_route("/test-only/boom", boom)
    response = api.get("/test-only/boom")
    error = _assert_error_shape(response, 500, "internal_error")
    assert error["message"] == "An unexpected error occurred."
    assert "hunter2" not in response.text and "RuntimeError" not in response.text


def test_database_connectivity_error_is_503(api):
    def database_down():
        raise OperationalError("SELECT 1", {}, Exception("connection refused to 10.0.0.5"))

    api.app.add_api_route("/test-only/db-down", database_down)
    response = api.get("/test-only/db-down")
    _assert_error_shape(response, 503, "service_unavailable")
    assert "10.0.0.5" not in response.text


@pytest.mark.parametrize("path", ["/v1/jobs", "/v1/uploads?filename=a.pdf"])
def test_errors_carry_a_request_id_header_on_every_route(api, path):
    response = api.post(path, content=b"", headers={"Content-Type": "text/plain"})
    assert response.status_code >= 400
    assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]


def test_connection_pool_exhaustion_is_503(api):
    from sqlalchemy.exc import TimeoutError as PoolTimeoutError

    def pool_exhausted():
        raise PoolTimeoutError("QueuePool limit of size 5 overflow 5 reached")

    api.app.add_api_route("/test-only/pool-exhausted", pool_exhausted)
    _assert_error_shape(api.get("/test-only/pool-exhausted"), 503, "service_unavailable")


def test_unreadable_body_maps_to_malformed_body(api):
    from fastapi import HTTPException

    def unreadable():
        raise HTTPException(status_code=400, detail="There was an error parsing the body")

    api.app.add_api_route("/test-only/unreadable", unreadable)
    error = _assert_error_shape(api.get("/test-only/unreadable"), 400, "malformed_body")
    assert error["message"] == "The request body could not be read or parsed."

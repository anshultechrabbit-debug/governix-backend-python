from app.core.metrics import Metrics


def test_metrics_have_bounded_path_labels_and_prometheus_format():
    metrics = Metrics()
    metrics.record("GET", "/documents/secret-id", 200, 0.125)
    metrics.record("GET", "/health", 200, 0.001)
    rendered = metrics.render()
    assert 'path="api"' in rendered
    assert 'path="/health"' in rendered
    assert "secret-id" not in rendered
    assert "governix_http_request_duration_seconds_total" in rendered

"""Rate limiting: token bucket, query endpoints, webhook signatures, client identity."""

import pytest

from app.config import settings
from app.core.rate_limit import MAX_TRACKED_CLIENTS, RateLimiter
from tests.conftest import signed_headers


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# ----------------------------------------------------------------- token bucket

def test_burst_then_limited_with_retry_after():
    clock = FakeClock()
    rl = RateLimiter(per_minute=60, burst=3, clock=clock)   # 1 token / second
    assert [rl.hit("a").remaining for _ in range(3)] == [2, 1, 0]
    d = rl.hit("a")
    assert not d.allowed and d.retry_after == 1


def test_bucket_refills_over_time_up_to_burst():
    clock = FakeClock()
    rl = RateLimiter(per_minute=60, burst=3, clock=clock)
    for _ in range(3):
        rl.hit("a")
    clock.t += 2.0
    assert rl.hit("a").allowed and rl.hit("a").allowed and not rl.hit("a").allowed
    clock.t += 3600
    assert rl.hit("a").remaining == 2   # capped at burst, not 3600 tokens


def test_clients_have_separate_buckets():
    rl = RateLimiter(per_minute=60, burst=1, clock=FakeClock())
    assert rl.hit("a").allowed and not rl.hit("a").allowed
    assert rl.hit("b").allowed


def test_retry_after_reflects_slow_refill():
    rl = RateLimiter(per_minute=6, burst=1, clock=FakeClock())   # one token / 10 s
    rl.hit("a")
    assert rl.hit("a").retry_after == 10


def test_tracked_clients_are_bounded():
    clock = FakeClock()
    rl = RateLimiter(per_minute=60, burst=1, clock=clock)
    for i in range(MAX_TRACKED_CLIENTS + 50):
        clock.t += 0.001
        rl.hit(f"client-{i}")
    assert len(rl._buckets) <= MAX_TRACKED_CLIENTS


# ----------------------------------------------------------------- query endpoints

@pytest.fixture
def tight_queries(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_per_minute", 60)
    monkeypatch.setattr(settings, "rate_limit_burst", 3)


def test_query_endpoint_limits_and_reports_headers(client, store, tight_queries):
    remaining = [client.get("/payments").headers["X-RateLimit-Remaining"] for _ in range(3)]
    assert remaining == ["2", "1", "0"]
    r = client.get("/payments")
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "1"
    assert r.headers["X-RateLimit-Limit"] == "3"


def test_payment_detail_shares_the_query_limit(client, store, tight_queries):
    for _ in range(3):
        client.get("/payments")
    assert client.get("/payments/whatever").status_code == 429


def test_health_and_ui_are_not_limited(client, store, tight_queries):
    for _ in range(3):
        client.get("/payments")
    assert client.get("/").status_code == 200
    assert client.get("/ui").status_code == 200


def test_zero_disables_query_limit(client, store, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_per_minute", 0)
    assert all(client.get("/payments").status_code == 200 for _ in range(100))
    assert "X-RateLimit-Limit" not in client.get("/payments").headers


# ----------------------------------------------------------------- webhooks

@pytest.fixture
def tight_signatures(monkeypatch):
    monkeypatch.setattr(settings, "invalid_signature_limit_per_minute", 3)


def test_valid_webhooks_are_never_limited(client, store, payment_payload, tight_signatures, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_burst", 1)
    for i in range(50):
        p = {**payment_payload, "payment_id": f"pay_{i}"}
        assert client.post("/webhooks/payments", json=p, headers=signed_headers(p)).status_code == 200


def test_invalid_signatures_get_401_then_429(client, store, payment_payload, tight_signatures):
    bad = {"X-Webhook-Signature": "sha256=deadbeef"}
    codes = [client.post("/webhooks/payments", json=payment_payload, headers=bad).status_code for _ in range(4)]
    assert codes == [401, 401, 401, 429]


def test_valid_webhook_still_accepted_after_invalid_ones_are_limited(client, store, payment_payload, tight_signatures):
    bad = {"X-Webhook-Signature": "sha256=deadbeef"}
    for _ in range(5):
        client.post("/webhooks/payments", json=payment_payload, headers=bad)
    r = client.post("/webhooks/payments", json=payment_payload, headers=signed_headers(payment_payload))
    assert r.status_code == 200 and r.json()["status"] == "accepted"


# ----------------------------------------------------------------- client identity

def test_x_forwarded_for_ignored_without_trusted_proxy(client, store, tight_queries):
    # A client rotating X-Forwarded-For must not get fresh buckets.
    codes = [client.get("/payments", headers={"X-Forwarded-For": f"10.0.0.{i}"}).status_code for i in range(4)]
    assert codes == [200, 200, 200, 429]


def test_x_forwarded_for_used_behind_trusted_proxy(client, store, tight_queries, monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxies", "testclient")   # TestClient's peer address
    for i in range(3):
        assert client.get("/payments", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 200
    assert client.get("/payments", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 429
    assert client.get("/payments", headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 200


def test_only_rightmost_forwarded_entry_is_trusted(client, store, tight_queries, monkeypatch):
    # The client controls everything left of what our proxy appended.
    monkeypatch.setattr(settings, "trusted_proxies", "testclient")
    for i in range(3):
        client.get("/payments", headers={"X-Forwarded-For": f"9.9.9.{i}, 1.1.1.1"})
    r = client.get("/payments", headers={"X-Forwarded-For": "8.8.8.8, 1.1.1.1"})
    assert r.status_code == 429

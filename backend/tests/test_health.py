def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    # Phase 11.5: /health now also reports pool + pg_stat_activity
    # counters so the keepalive cron can grep idle_in_txn out of the
    # log for the connection-leak watch routine. Verify shape, not
    # exact values (those depend on the test session state).
    assert "pool" in body
    for key in ("size", "checked_in", "checked_out", "overflow"):
        assert key in body["pool"]
    assert "db" in body
    for key in ("idle_in_txn", "active", "idle", "total"):
        assert key in body["db"]

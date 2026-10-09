"""Tests for TestStubSyncServer (the stub SyncServer, docs/design-api.md)."""

import pytest
import requests

from stub_sync_server import KEY_TTL_SECONDS, TestStubSyncServer


@pytest.fixture
def server():
    with TestStubSyncServer() as s:
        yield s


@pytest.fixture
def session(server):
    return requests.Session()


def start(server, sess):
    resp = sess.post(server.url + "/v1/sync/start")
    assert resp.status_code == 200
    return resp.json()["key"]


# -- session lifecycle (§2, §6.1, §6.4) -------------------------------------------

def test_start_sync_returns_key(server, session):
    data = session.post(server.url + "/v1/sync/start").json()
    assert data["key"]
    assert data["key_ttl_seconds"] == KEY_TTL_SECONDS


def test_start_sync_conflicts_while_active(server, session):
    start(server, session)
    resp = session.post(server.url + "/v1/sync/start")
    assert resp.status_code == 409
    assert resp.json()["code"] == "session_conflict"


def test_stop_sync_invalidates_key(server, session):
    key = start(server, session)
    resp = session.post(server.url + "/v1/sync/stop", headers={"Authorization": key})
    assert resp.status_code == 200
    resp = session.get(server.url + "/v1/collect/status", headers={"Authorization": key})
    assert resp.status_code == 401
    # the slot is idle again
    assert session.post(server.url + "/v1/sync/start").status_code == 200


def test_missing_key_is_rejected(server, session):
    resp = session.get(server.url + "/v1/collect/status")
    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_key"


def test_wrong_key_is_rejected(server, session):
    start(server, session)
    resp = session.get(server.url + "/v1/collect/status", headers={"Authorization": "nope"})
    assert resp.status_code == 401


# -- collect flow (§6.2) ------------------------------------------------------------

def test_collect_reports_empty_remote_set(server, session):
    key = start(server, session)
    headers = {"Authorization": key}
    # 202 = started; the body carries the job id + progress (0.0 at start).
    resp = session.post(server.url + "/v1/collect", headers=headers)
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "running"
    assert body["job"]
    assert body["progress"] == 0.0

    # one "running" poll by default (202 + progress), then "done" with an empty set
    resp = session.get(server.url + "/v1/collect/status", headers=headers)
    assert resp.status_code == 202
    assert resp.json()["status"] == "running"
    assert 0.0 < resp.json()["progress"] < 1.0

    resp = session.get(server.url + "/v1/collect/status", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"status": "done", "result": {}, "extra_copies": []}


def test_collect_status_without_collect_is_404(server, session):
    key = start(server, session)
    resp = session.get(server.url + "/v1/collect/status", headers={"Authorization": key})
    assert resp.status_code == 404
    assert resp.json()["code"] == "no_collection"


def test_second_collect_conflicts(server, session):
    key = start(server, session)
    headers = {"Authorization": key}
    assert session.post(server.url + "/v1/collect", headers=headers).status_code == 202
    resp = session.post(server.url + "/v1/collect", headers=headers)
    assert resp.status_code == 409
    assert resp.json()["code"] == "collection_in_progress"


def test_running_polls_are_configurable(server, session):
    key = start(server, session)
    headers = {"Authorization": key}
    server.set_collect_running_polls(3)
    session.post(server.url + "/v1/collect", headers=headers)
    progresses = []
    for _ in range(3):
        r = session.get(server.url + "/v1/collect/status", headers=headers)
        assert r.status_code == 202
        progresses.append(r.json()["progress"])
    # progress is monotonic non-decreasing and <1.0 while running
    assert progresses == sorted(progresses)
    assert all(0.0 < p < 1.0 for p in progresses)
    assert session.get(server.url + "/v1/collect/status", headers=headers).status_code == 200


# -- atomic operations (§6.3) ----------------------------------------------------------

def test_operation_endpoints_are_friendly(server, session):
    key = start(server, session)
    headers = {"Authorization": key}
    base = server.url

    r = session.post(base + "/v1/upload", headers=headers, json={
        "hash": "h1", "album": "2026/4/A/B", "record": {"year": 2026},
        "content_type": "image/jpeg", "file": "aGVsbG8=",
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["asset_id"]

    assert session.post(base + "/v1/delete", headers=headers,
                        json={"hash": "h1"}).json()["ok"] is True
    assert session.post(base + "/v1/delete", headers=headers,
                        json={"asset_id": "dup-1"}).json()["ok"] is True
    assert session.post(base + "/v1/move", headers=headers,
                        json={"hash": "h1", "album": "2026/4/A/B", "record": {}}
                        ).json()["ok"] is True
    assert session.post(base + "/v1/album/create", headers=headers,
                        json={"identity": "2026/4/A/B", "name": "A — B"}
                        ).json()["ok"] is True
    assert session.post(base + "/v1/album/delete", headers=headers,
                        json={"identity": "2026/4/A/B"}).json()["ok"] is True
    assert session.post(base + "/v1/album/rename", headers=headers,
                        json={"identity": "2026/4/A/B", "new_name": "A — C"}
                        ).json()["ok"] is True


def test_upload_payload_is_recorded(server, session):
    key = start(server, session)
    headers = {"Authorization": key}
    session.post(server.url + "/v1/upload", headers=headers, json={
        "hash": "h1", "album": "2026/4/A/B",
        "record": {"year": 2026, "month": 4},
        "content_type": "image/jpeg", "file": "aGVsbG8=",
    })
    up = server.state.uploads["h1"]
    assert up["album"] == "2026/4/A/B"
    assert up["record"] == {"year": 2026, "month": 4}
    assert up["content_type"] == "image/jpeg"
    assert up["file_size"] == 5


def test_move_returns_album_and_record(server, session):
    key = start(server, session)
    headers = {"Authorization": key}
    rec = {"year": 2026, "month": 4, "event": "A", "subevent": "B"}
    r = session.post(server.url + "/v1/move", headers=headers,
                     json={"hash": "h1", "album": "2026/4/A/B", "record": rec})
    data = r.json()
    assert data["ok"] is True
    assert data["moved"] == "h1"
    assert data["album"] == "2026/4/A/B"
    assert data["record"] == rec


# -- logging ----------------------------------------------------------------------------

def test_every_call_is_logged(server, session):
    key = start(server, session)
    headers = {"Authorization": key}
    session.post(server.url + "/v1/collect", headers=headers)
    session.get(server.url + "/v1/collect/status", headers=headers)
    session.get(server.url + "/v1/collect/status", headers=headers)
    session.post(server.url + "/v1/sync/stop", headers=headers)

    paths = [c["path"] for c in server.state.requests_log]
    assert paths == [
        "/v1/sync/start",
        "/v1/collect",
        "/v1/collect/status",
        "/v1/collect/status",
        "/v1/sync/stop",
    ]
    assert all(c["status"] < 500 for c in server.state.requests_log)
    # the logged body of the collect/status polls is None (GET carries no body)
    assert server.calls("/v1/collect/status")[0]["body"] is None

"""Password login for remote access: off by default, open on this machine,
required for anything relayed by a tunnel."""
from fastapi.testclient import TestClient

from app import auth, config
from app.api import app

TUNNEL = {"host": "work.example.org", "cf-connecting-ip": "203.0.113.9"}


def _on(monkeypatch, pw="s3cret-pass"):
    h = auth.make_hash(pw)
    monkeypatch.setattr(config.osutil, "secret", lambda name, env_file=None: h if name == "JOURNAL_PASSWORD_HASH" else "")
    auth._FAILS.clear()


def test_no_password_means_no_login(env):
    c = TestClient(app, base_url="http://work.example.org")
    assert c.get("/api/v1/days", headers=TUNNEL).status_code == 200


def test_tunnel_requests_need_the_password_but_local_ones_do_not(env, monkeypatch):
    _on(monkeypatch)
    c = TestClient(app, base_url="http://work.example.org", follow_redirects=False)
    assert c.get("/api/v1/days", headers=TUNNEL).status_code == 401
    r = c.get("/timeline?x=1", headers=TUNNEL)
    assert r.status_code == 302 and r.headers["location"] == "/login?next=/timeline%3Fx%3D1"
    # straight to 127.0.0.1 on this machine: open
    local = TestClient(app, base_url="http://127.0.0.1:8799", client=("127.0.0.1", 5000))
    assert local.get("/api/v1/days").status_code == 200
    # localhost Host but relayed (header present): still locked
    assert local.get("/api/v1/days", headers={"x-forwarded-for": "1.2.3.4"}).status_code == 401

    assert c.post("/login", data={"password": "nope", "next": "/"}, headers=TUNNEL).status_code == 401
    r = c.post("/login", data={"password": "s3cret-pass", "next": "//evil.com"}, headers=TUNNEL)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "httponly" in r.headers["set-cookie"].lower() and "secure" in r.headers["set-cookie"].lower()
    c.cookies.set(auth.COOKIE, r.cookies[auth.COOKIE])
    assert c.get("/api/v1/days", headers=TUNNEL).status_code == 200
    # a forged or expired cookie is not a session
    c.cookies.set(auth.COOKIE, "9999999999.deadbeef")
    assert c.get("/api/v1/days", headers=TUNNEL).status_code == 401
    assert not auth.valid("1." + "0" * 64)


def test_repeated_wrong_passwords_are_throttled(env, monkeypatch):
    _on(monkeypatch)
    monkeypatch.setattr(auth, "MAX_FAILS", 3)
    import asyncio
    async def no_wait(_):
        return None
    monkeypatch.setattr(asyncio, "sleep", no_wait)
    c = TestClient(app, base_url="http://work.example.org", follow_redirects=False)
    for _ in range(3):
        assert c.post("/login", data={"password": "x"}, headers=TUNNEL).status_code == 401
    assert c.post("/login", data={"password": "s3cret-pass"}, headers=TUNNEL).status_code == 429


def test_hash_round_trip():
    # not hex, so a random salt can't contain it by chance
    h = auth.make_hash("s3cret-pass")
    assert h.startswith("pbkdf2_sha256$") and "s3cret-pass" not in h
    assert auth.check_password("s3cret-pass", h) and not auth.check_password("s3cret-pasz", h)
    assert not auth.check_password("s3cret-pass", "garbage")

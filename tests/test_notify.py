"""Discord 推送：长摘要不能被静默截断，文本里的 @ 不能真的 @ 人，webhook URL 不能漏出去。"""

import httpx
import pytest

from jha import notify

URL = "https://discord.com/api/webhooks/1/SECRET_TOKEN"


class Post:
    def __init__(self, status=204):
        self.status = status
        self.calls = []

    def __call__(self, url, json, timeout):
        self.calls.append(json)
        return httpx.Response(self.status, text="rate limited" if self.status == 429 else "")


@pytest.fixture
def post(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", URL)
    p = Post()
    monkeypatch.setattr(notify.httpx, "post", p)
    return p


def test_long_digest_is_split_not_truncated(post):
    text = "\n".join(f"· Company {i} — AI Engineer https://jobs.example.com/{i}" for i in range(200))
    assert notify.send(text).sent
    assert len(post.calls) > 1
    assert all(len(c["content"]) <= notify.MAX_LEN for c in post.calls)
    assert "\n".join(c["content"] for c in post.calls) == text


def test_mentions_and_link_previews_are_off(post):
    notify.send("@everyone 面试邀请")
    body = post.calls[0]
    assert body["allowed_mentions"] == {"parse": []}
    assert body["flags"] & notify.SUPPRESS_EMBEDS


def test_http_error_is_reported(post):
    post.status = 429
    res = notify.send("hi")
    assert not res.sent and "429" in res.detail


def test_network_error_does_not_leak_the_webhook_url(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", URL)

    def boom(url, json, timeout):
        raise httpx.ConnectError(f"cannot reach {url}")

    monkeypatch.setattr(notify.httpx, "post", boom)
    res = notify.send("hi")
    assert not res.sent and "SECRET_TOKEN" not in res.detail


def test_not_configured_sends_nothing(monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(notify.httpx, "post", lambda *a, **k: pytest.fail("不该发请求"))
    assert not notify.send("hi").sent

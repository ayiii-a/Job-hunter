import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--live", action="store_true", default=False,
        help="跑打真实 ATS 接口的冒烟测试（默认跳过）",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "live: 需要网络，打真实 ATS 接口")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        return
    skip = pytest.mark.skip(reason="需要 --live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _no_real_webhook(monkeypatch):
    """config 在 import 时就加载 .env。不摘掉的话，批准执行 send_notification 的测试会往你的真频道发消息
    （实测每跑一次 pytest 发两条 "x"）。要测推送的测试自己 setenv 一个假地址。"""
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)

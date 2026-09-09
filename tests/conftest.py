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

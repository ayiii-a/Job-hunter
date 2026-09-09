"""resolve_ats 里不联网的那部分：候选提取和 token 猜测。

真实接口的验证走 tests/test_ats_live.py（默认跳过，需要 --live）。
"""

from jha.tools import resolve_ats as ra


def extract(text):
    return {(c.ats_type, c.token) for c in ra._extract(text, "html")}


def test_greenhouse_new_domain():
    assert ("greenhouse", "databricks") in extract(
        'href="https://job-boards.greenhouse.io/databricks/jobs/123"'
    )


def test_greenhouse_legacy_domain():
    # 旧域名 301 到新域名，但历史页面里还大量存在，必须照样认
    assert ("greenhouse", "stripe") in extract('src="https://boards.greenhouse.io/stripe"')


def test_greenhouse_embed_for_param():
    # 最常见的嵌入形态：公司自己的域名 + 一段 JS，token 藏在 for= 里
    html = '<script src="https://boards.greenhouse.io/embed/job_board/js?for=acmeco"></script>'
    assert ("greenhouse", "acmeco") in extract(html)


def test_greenhouse_api_url():
    assert ("greenhouse", "figma") in extract(
        "https://boards-api.greenhouse.io/v1/boards/figma/jobs?content=true"
    )


def test_lever():
    assert ("lever", "exampleco") in extract('href="https://jobs.lever.co/exampleco/abc-123"')


def test_lever_eu_domain():
    assert ("lever", "euco") in extract("https://jobs.eu.lever.co/euco")


def test_ashby():
    assert ("ashby", "ramp") in extract('"https://jobs.ashbyhq.com/ramp/opening"')


def test_workday():
    assert ("workday", "bigco") in extract("https://bigco.wd1.myworkdayjobs.com/careers")


def test_structural_path_segments_are_not_tokens():
    # embed / js / job_board 是 ATS 自己的路径结构，不是公司标识
    tokens = {t for _, t in extract("https://boards.greenhouse.io/embed/job_board?for=real")}
    assert "embed" not in tokens
    assert "real" in tokens


def test_no_false_positives_on_unrelated_html():
    assert extract("<html><body>We are hiring! Email jobs@acme.com</body></html>") == set()


def test_dedupes_repeated_mentions():
    html = "boards.greenhouse.io/acme " * 5
    assert len([c for c in ra._extract(html, "html") if c.ats_type == "greenhouse"]) == 1


def test_guess_tokens_from_company_name():
    assert "acmecorp" in ra.guess_tokens("Acme Corp")
    assert "acme-corp" in ra.guess_tokens("Acme Corp")


def test_guess_tokens_strips_common_suffix():
    assert "acme" in ra.guess_tokens("Acme Inc")


def test_guess_tokens_handles_punctuation():
    guesses = ra.guess_tokens("Foo.Bar & Co!")
    assert "foobarco" in guesses


def test_merge_dedupes_across_origins():
    # 同一个 token 会在最终 URL 和页面 HTML 里各命中一次
    out = []
    ra._merge(out, [ra.Candidate("ashby", "ramp", "html")])
    ra._merge(out, [ra.Candidate("ashby", "ramp", "url")])
    assert len(out) == 1
    assert out[0].origin == "url"       # 保留更可信的来源


def test_merge_does_not_downgrade_origin():
    out = []
    ra._merge(out, [ra.Candidate("ashby", "ramp", "url")])
    ra._merge(out, [ra.Candidate("ashby", "ramp", "guess")])
    assert len(out) == 1
    assert out[0].origin == "url"


def test_merge_is_case_insensitive_on_token():
    out = []
    ra._merge(out, [ra.Candidate("greenhouse", "Acme", "html")])
    ra._merge(out, [ra.Candidate("greenhouse", "acme", "html")])
    assert len(out) == 1


def test_target_url_is_read_even_when_page_fetch_fails(monkeypatch):
    # ATS 的板子地址常常 302 跳回公司自己的 careers 页，token 就丢了。
    # 用户手里那个 URL 本身就是证据，抓页面失败也不能把它扔掉。
    import httpx

    def boom(client, url):
        raise httpx.ConnectError("simulated")

    monkeypatch.setattr(ra, "fetch_page", boom)
    got = ra.resolve("https://job-boards.greenhouse.io/databricks", verify=False)
    assert ("greenhouse", "databricks") in {(c.ats_type, c.token) for c in got}


def test_format_yaml_entry_reminds_about_email_domains():
    entry = ra.format_yaml_entry(
        "Acme", ra.Candidate("greenhouse", "acme", "url"), "https://acme.com/careers"
    )
    assert "board_token: acme" in entry
    assert "email_domains" in entry

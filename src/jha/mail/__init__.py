"""Phase 5：邮件。只读。

    imap.py       读取。EXAMINE + BODY.PEEK + 白名单守卫——三层保证不改邮箱
    prefilter.py  确定性预过滤，不过 LLM
    classify.py   第二层分类：逐封一次调用，正文不进 agent 上下文，那一层没有工具
    match.py      确定性匹配到投递记录
    policy.py     按误判代价分级：什么自动写，什么必须人工确认
    pipeline.py   把上面串起来 + 人工确认队列
"""

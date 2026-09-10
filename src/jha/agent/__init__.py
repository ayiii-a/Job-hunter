"""Agent loop：模型决定做什么，Phase 0/1 的确定性代码决定怎么做。

安全属性靠工具注册表保证，不靠 prompt —— 详见 tools.py 的模块说明。
"""

from .client import (
    AgentClient, Budget, BudgetExceeded, MissingAPIKey,
    UNPRICED_MODELS, price_of, spend_summary,
)
from .loop import RunResult, Step, run
from .persistence import (
    RunRecorder, approval_counts, cost_by_schedule, recent_runs, run_steps,
)
from .tools import REGISTRY, Permission

__all__ = [
    "AgentClient", "Budget", "BudgetExceeded", "MissingAPIKey",
    "UNPRICED_MODELS", "price_of", "spend_summary",
    "RunResult", "Step", "run", "REGISTRY", "Permission",
    "RunRecorder", "approval_counts", "cost_by_schedule", "recent_runs", "run_steps",
]

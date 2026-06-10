# models/strategies/__init__.py
from models.strategies.strategy_v1 import StrategyV1

# ACTIVE_STRATEGY — เปลี่ยนตรงนี้เมื่อ upgrade version
# !! ต้อง backtest ผ่านก่อนเสมอ !!
ACTIVE_STRATEGY = StrategyV1

__all__ = ['StrategyV1', 'ACTIVE_STRATEGY']
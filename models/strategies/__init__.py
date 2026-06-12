<<<<<<< HEAD
# models/strategies/__init__.py
from models.strategies.strategy_v1 import StrategyV1

# ACTIVE_STRATEGY — เปลี่ยนตรงนี้เมื่อ upgrade version
# !! ต้อง backtest ผ่านก่อนเสมอ !!
ACTIVE_STRATEGY = StrategyV1

=======
# models/strategies/__init__.py
from models.strategies.strategy_v1 import StrategyV1

# ACTIVE_STRATEGY — เปลี่ยนตรงนี้เมื่อ upgrade version
# !! ต้อง backtest ผ่านก่อนเสมอ !!
ACTIVE_STRATEGY = StrategyV1

>>>>>>> 98ac82b18ee8d71be376450f278ba77dde0c1c3e
__all__ = ['StrategyV1', 'ACTIVE_STRATEGY']
# รันใน python terminal
from bot.risk_manager import RiskManager
rm = RiskManager()

result = rm.check_spread("XAUUSDm")
print(result)
# ✅ OK | XAUUSDm spread=0.25000 limit=0.80000 (31%)

summary = rm.get_spread_summary()
print(summary)
# {'XAUUSDm': {'current': 0.25, 'avg_20': 0.27, 'limit': 0.80, 'is_widening': False}}
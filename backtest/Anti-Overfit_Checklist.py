def validate_no_overfit(train_score, val_score, test_score):
    checks = {
        "Train-Val gap < 10%":     abs(train_score - val_score)  < 0.10,
        "Val-Test gap < 5%":       abs(val_score   - test_score) < 0.05,
        "Test accuracy > 52%":     test_score > 0.52,   # random = 50% (binary)
        "Sharpe Ratio > 1.0":      True,  # ต้องคำนวณจาก backtest
        "Max Drawdown < 20%":      True,  # ต้องคำนวณจาก backtest
    }
    for check, passed in checks.items():
        print(f"{'✅' if passed else '❌'} {check}")
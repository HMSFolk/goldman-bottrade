<<<<<<< HEAD
# scripts/daily_checklist.py
"""
ทำทุกวันระหว่าง paper trading
สร้างเป็น habit ก่อน live
"""
from datetime import datetime, timezone

def morning_check():
    """ตรวจตอนเช้า — ก่อนตลาดเปิด"""
    print(f"\n{'='*55}")
    print(f"  🌅 Morning Check — "
          f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}")
    print(f"{'='*55}")

    checks = [
        ("nssm status TradingBot", "Bot service running"),
        ("Get-Content logs\\bot.log -Tail 5", "No errors in log"),
        ("python scripts\\paper_trading_monitor.py", "Yesterday P&L"),
    ]

    for cmd, desc in checks:
        print(f"  □ {desc}")
        print(f"    > {cmd}")

    print(f"\n  Questions to ask:")
    print(f"  □ บอทเทรดกี่ครั้งเมื่อวาน?")
    print(f"  □ มี error ใน logs\\errors.log ไหม?")
    print(f"  □ Signal quality เป็นยังไง?")


def evening_review():
    """ทบทวนตอนเย็น — หลังตลาด NY ปิด"""
    print(f"\n{'='*55}")
    print(f"  🌆 Evening Review")
    print(f"{'='*55}")
    print(f"  □ Review trades วันนี้")
    print(f"    > python scripts\\paper_trading_monitor.py --daily 1")
    print(f"  □ ตรวจ win/loss ratio ตรงกับ backtest ไหม")
    print(f"  □ ดู signal confidence distribution")
    print(f"  □ ตรวจ SL hit vs TP hit ratio")
    print(f"  □ บันทึกสิ่งที่สังเกตเห็น")


def week_review():
    """ทบทวนรายสัปดาห์"""
    print(f"\n{'='*55}")
    print(f"  📊 Weekly Review")
    print(f"{'='*55}")
    print(f"  > python scripts\\paper_trading_monitor.py --weekly")
    print(f"\n  คำถามสำคัญ:")
    print(f"  □ Performance ใกล้เคียง backtest แค่ไหน?")
    print(f"    (Sharpe, Win Rate, Profit Factor)")
    print(f"  □ Symbol ไหนทำได้ดีที่สุด?")
    print(f"  □ Session ไหนทำได้ดีที่สุด (London/NY/Overlap)?")
    print(f"  □ มี filter ที่ควรปรับไหม? (confidence threshold)")
    print(f"  □ SL/TP เหมาะสมกับ market ปัจจุบันไหม?")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        if "morning" in sys.argv[1]:
            morning_check()
        elif "evening" in sys.argv[1]:
            evening_review()
        elif "week" in sys.argv[1]:
            week_review()
    else:
        morning_check()
=======
# scripts/daily_checklist.py
"""
ทำทุกวันระหว่าง paper trading
สร้างเป็น habit ก่อน live
"""
from datetime import datetime, timezone

def morning_check():
    """ตรวจตอนเช้า — ก่อนตลาดเปิด"""
    print(f"\n{'='*55}")
    print(f"  🌅 Morning Check — "
          f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}")
    print(f"{'='*55}")

    checks = [
        ("nssm status TradingBot", "Bot service running"),
        ("Get-Content logs\\bot.log -Tail 5", "No errors in log"),
        ("python scripts\\paper_trading_monitor.py", "Yesterday P&L"),
    ]

    for cmd, desc in checks:
        print(f"  □ {desc}")
        print(f"    > {cmd}")

    print(f"\n  Questions to ask:")
    print(f"  □ บอทเทรดกี่ครั้งเมื่อวาน?")
    print(f"  □ มี error ใน logs\\errors.log ไหม?")
    print(f"  □ Signal quality เป็นยังไง?")


def evening_review():
    """ทบทวนตอนเย็น — หลังตลาด NY ปิด"""
    print(f"\n{'='*55}")
    print(f"  🌆 Evening Review")
    print(f"{'='*55}")
    print(f"  □ Review trades วันนี้")
    print(f"    > python scripts\\paper_trading_monitor.py --daily 1")
    print(f"  □ ตรวจ win/loss ratio ตรงกับ backtest ไหม")
    print(f"  □ ดู signal confidence distribution")
    print(f"  □ ตรวจ SL hit vs TP hit ratio")
    print(f"  □ บันทึกสิ่งที่สังเกตเห็น")


def week_review():
    """ทบทวนรายสัปดาห์"""
    print(f"\n{'='*55}")
    print(f"  📊 Weekly Review")
    print(f"{'='*55}")
    print(f"  > python scripts\\paper_trading_monitor.py --weekly")
    print(f"\n  คำถามสำคัญ:")
    print(f"  □ Performance ใกล้เคียง backtest แค่ไหน?")
    print(f"    (Sharpe, Win Rate, Profit Factor)")
    print(f"  □ Symbol ไหนทำได้ดีที่สุด?")
    print(f"  □ Session ไหนทำได้ดีที่สุด (London/NY/Overlap)?")
    print(f"  □ มี filter ที่ควรปรับไหม? (confidence threshold)")
    print(f"  □ SL/TP เหมาะสมกับ market ปัจจุบันไหม?")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        if "morning" in sys.argv[1]:
            morning_check()
        elif "evening" in sys.argv[1]:
            evening_review()
        elif "week" in sys.argv[1]:
            week_review()
    else:
        morning_check()
>>>>>>> 98ac82b18ee8d71be376450f278ba77dde0c1c3e
        evening_review()
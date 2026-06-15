# features/order_flow.py
import pandas as pd

def add_order_flow_features(df: pd.DataFrame) -> pd.DataFrame:
    # ตัวอย่างการคำนวณง่ายๆ: ดูว่าแท่งนี้ Volume เยอะกว่าค่าเฉลี่ยไหม
    df['volume_sma_20'] = df['tick_volume'].rolling(20).mean()
    df['is_high_volume'] = (df['tick_volume'] > df['volume_sma_20']).astype(int)
    
    return df
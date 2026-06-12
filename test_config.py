<<<<<<< HEAD
# test_config.py — รันเพื่อตรวจทุกอย่าง
import yaml, os
from dotenv import load_dotenv
import MetaTrader5 as mt5

load_dotenv()

# 1. ตรวจ config.yaml
with open("config.yaml") as f:
    cfg = yaml.safe_load(f)
print("✅ config.yaml loaded")
print(f"   Symbols: {cfg['trading']['symbols']}")
print(f"   Risk:    {cfg['risk']['risk_per_trade']:.1%}")

# 2. ตรวจ .env
required_env = ['MT5_LOGIN','MT5_PASSWORD','MT5_SERVER']
for key in required_env:
    val = os.getenv(key)
    if not val:
        print(f"❌ .env missing: {key}")
    else:
        print(f"✅ .env {key}: {'*'*8}")

# 3. ตรวจ MT5
if mt5.initialize():
    ok = mt5.login(
        int(os.getenv('MT5_LOGIN')),
        password=os.getenv('MT5_PASSWORD'),
        server=os.getenv('MT5_SERVER'),
    )
    if ok:
        acc = mt5.account_info()
        print(f"✅ MT5 connected: {acc.name} ${acc.balance:,.2f}")
    else:
        print(f"❌ MT5 login failed: {mt5.last_error()}")
    mt5.shutdown()
else:
    print(f"❌ MT5 initialize failed: {mt5.last_error()}")

# 4. ตรวจ Python packages
packages = ['xgboost','lightgbm','torch','streamlit','vectorbt']
for pkg in packages:
    try:
        __import__(pkg)
        print(f"✅ {pkg}")
    except ImportError:
=======
# test_config.py — รันเพื่อตรวจทุกอย่าง
import yaml, os
from dotenv import load_dotenv
import MetaTrader5 as mt5

load_dotenv()

# 1. ตรวจ config.yaml
with open("config.yaml") as f:
    cfg = yaml.safe_load(f)
print("✅ config.yaml loaded")
print(f"   Symbols: {cfg['trading']['symbols']}")
print(f"   Risk:    {cfg['risk']['risk_per_trade']:.1%}")

# 2. ตรวจ .env
required_env = ['MT5_LOGIN','MT5_PASSWORD','MT5_SERVER']
for key in required_env:
    val = os.getenv(key)
    if not val:
        print(f"❌ .env missing: {key}")
    else:
        print(f"✅ .env {key}: {'*'*8}")

# 3. ตรวจ MT5
if mt5.initialize():
    ok = mt5.login(
        int(os.getenv('MT5_LOGIN')),
        password=os.getenv('MT5_PASSWORD'),
        server=os.getenv('MT5_SERVER'),
    )
    if ok:
        acc = mt5.account_info()
        print(f"✅ MT5 connected: {acc.name} ${acc.balance:,.2f}")
    else:
        print(f"❌ MT5 login failed: {mt5.last_error()}")
    mt5.shutdown()
else:
    print(f"❌ MT5 initialize failed: {mt5.last_error()}")

# 4. ตรวจ Python packages
packages = ['xgboost','lightgbm','torch','streamlit','vectorbt']
for pkg in packages:
    try:
        __import__(pkg)
        print(f"✅ {pkg}")
    except ImportError:
>>>>>>> 98ac82b18ee8d71be376450f278ba77dde0c1c3e
        print(f"❌ {pkg} not installed")
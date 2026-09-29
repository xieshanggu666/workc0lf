import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

DATABASE_URL = f"sqlite:///{DATA_DIR / 'bunker.db'}"
SECRET_KEY = "bunker-survival-dev-secret-2026"
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 720

# 游戏初始参数
INITIAL_RESOURCES = {"food": 300, "water": 300, "power": 200, "oxygen": 200}
INITIAL_DAY = 1
SURVIVAL_TARGET_DAY = 120
MAX_DAYS = 200
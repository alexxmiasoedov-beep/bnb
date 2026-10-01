"""Все настройки в одном месте. Любую можно переопределить переменной окружения FM_<ИМЯ>."""
import os


def _env(name, default, cast=str):
    v = os.environ.get("FM_" + name)
    return cast(v) if v is not None else default


# --- сеть -------------------------------------------------------------------
# Публичный узел NodeReal с полной историей; свой ключ (NodeReal/Ankr/QuickNode)
# снимет лимиты частоты: FM_RPC_URL=https://...
RPC_URL = _env("RPC_URL", "https://bsc-mainnet.nodereal.io/v1/64a9df0874fb4a93b9d0a3849de012d3")
RPC_PAUSE = _env("RPC_PAUSE", 0.25, float)      # пауза между запросами, с
LOG_CHUNK = _env("LOG_CHUNK", 4000, int)         # блоков в одном eth_getLogs
DB_PATH = _env("DB_PATH", "data/fourmeme.sqlite")

# --- контракты и события Four.meme ----------------------------------------
TOKEN_MANAGER = "0x5c952063c7fc8610ffdb798152d69f0b9550762b"   # TokenManager2
HELPER = "0xf251f83e40a78868fcfa3fa4599dad6494e46034"          # TokenManagerHelper3
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
PANCAKE_V2_FACTORY = "0xca143ce32fe78f1f7019d7d551a6402fc5350c73"

T_CREATE = "0x396d5e902b675b032348d3d2e9517ee8f0c4a926603fbc075d3d282ff00cad20"    # TokenCreate
T_BUY = "0x7db52723a3b2cdd6164364b3b766e65e540d7be48ffa89582956d8eaebe62942"       # TokenPurchase
T_SELL = "0x0a5575b3648bae2210cee56bf33254cc1ddfbc7bf637c0af2ac18b14fb1bae19"      # TokenSale
T_LIQ = "0xc18aa71171b358b706fe3dd345299685ba21a5316c66ffa9e319268b033c44b0"       # LiquidityAdded
T_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
T_SYNC = "0x1c411e9a96e071241c2f21f7726b17ae89e3cab4c78be50e062b03a9fffbbad1"

# --- бондинг-кривая (токены за BNB) ----------------------------------------
# constant product с виртуальными резервами, проверено на сделках 01.10.2026:
# старт 5,74e-9 BNB за токен, 800 млн токенов на кривой, выпуск на PancakeSwap
# при 18 BNB. Виртуальный резерв BNB выводится из этих трёх чисел.
CURVE_P0 = 5.74e-9
CURVE_OFFERS = 800e6
CURVE_FUNDS = 18.0
TOTAL_SUPPLY = 1e9
TRADE_FEE = 0.01           # комиссия Four.meme, 1% с каждой сделки
PANCAKE_FEE = 0.0025

# --- правила копирования ----------------------------------------------------
ENTRY_BNB = _env("ENTRY_BNB", 0.1, float)        # фиксированный вход
GAS_BNB = _env("GAS_BNB", 0.00005, float)        # газ на одну транзакцию
LATENCIES = (1, 2, 3)                            # задержка копии, блоков (~0,45 с)
MAIN_LATENCY = _env("MAIN_LATENCY", 2, int)      # по ней ранжируем
STOP_LOSS = _env("STOP_LOSS", 0.70, float)       # −70% от входа → выходим целиком
IDLE_HOURS = _env("IDLE_HOURS", 24.0, float)     # нет сделок в токене столько часов → выходим
POSTMIG_HOURS = _env("POSTMIG_HOURS", 72.0, float)  # горизонт слежения после выхода на PancakeSwap

# --- фильтры кошельков ------------------------------------------------------
MIN_SIGNALS = _env("MIN_SIGNALS", 30, int)       # первых покупок в обучающем окне
SNIPE_BLOCKS = 3                                 # покупка ≤3 блоков от создания = снайп
MAX_SNIPE_SHARE = 0.5
MAX_CREATOR_SHARE = 0.3
MAX_TRADES_PER_DAY = 100
MIN_MEDIAN_HOLD_SEC = 45
MAX_BIG_IMPACT_SHARE = 0.3       # доля входов, где его покупка сдвинула цену >15%
BIG_IMPACT = 0.15
BAIT_BLOCKS = 10                 # продал ≥50% за ≤10 блоков после покупки
MAX_BAIT_SHARE = 0.3
MIN_PROFIT_FACTOR = 1.5
MIN_POS_WEEKS = 0.75             # доля прибыльных недель
TEST_SHARE = 0.25                # последняя четверть окна — проверка
CLUSTER_MIN_COMMON = 5           # общих входов в одном блоке → один игрок
PORTFOLIO_SIZE = _env("PORTFOLIO_SIZE", 15, int)

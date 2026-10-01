"""Симуляция копирования кошелька по нашим правилам.

Правила:
- копируем только ПЕРВУЮ покупку кошелька в токене, всегда на ENTRY_BNB,
  докупки кошелька игнорируем;
- кошелёк продал X% своего остатка (на кривой или переводом/продажей на
  PancakeSwap) → мы продаём X% своего остатка;
- исполнение с задержкой k блоков: берём состояние кривой/пары на начало
  блока (блок сигнала + k), с проскальзыванием по формуле кривой и комиссией;
- страховка: стоп-лосс STOP_LOSS, выход при простое токена IDLE_HOURS,
  принудительное закрытие на горизонте POSTMIG_HOURS после PancakeSwap;
- в конце окна открытая позиция оценивается по цене продажи в кривую/пару.
"""
import bisect
from collections import defaultdict

from . import config, db

ZERO = "0x" + "0" * 40


class Curve:
    """Кривая Four.meme: (X)(Y)=k, Y = funds + B (виртуальный резерв BNB)."""

    def __init__(self, max_funds):
        m, p0 = config.CURVE_OFFERS, config.CURVE_P0
        self.fm = max_funds or config.CURVE_FUNDS
        x0 = m * self.fm / (self.fm - m * p0)
        self.b = p0 * x0
        self.k = x0 * self.b

    def spot(self, funds):
        y = funds + self.b
        return y * y / self.k

    def buy(self, funds, bnb_in):
        """Сколько токенов получим за bnb_in (комиссия сверху 1%)."""
        cost = bnb_in / (1 + config.TRADE_FEE)
        if funds + cost > self.fm:
            return None
        y = funds + self.b
        return self.k / y - self.k / (y + cost)

    def sell(self, funds, tokens):
        y = funds + self.b
        y2 = self.k / (self.k / y + tokens)
        return max(0.0, (y - y2)) * (1 - config.TRADE_FEE)


def pancake_sell(r_tok, r_bnb, tokens):
    if r_tok <= 0:
        return 0.0
    q = tokens * (1 - config.PANCAKE_FEE)
    return r_bnb * q / (r_tok + q)


class Data:
    """Всё нужное для симуляции, загруженное из SQLite в память."""

    def __init__(self, con=None):
        con = con or db.connect()
        self.start = int(db.meta_get(con, "start_block"))
        self.end = int(db.meta_get(con, "last_block"))
        a = con.execute("SELECT block, ts FROM anchors ORDER BY block").fetchall()
        self._ab, self._at = [x[0] for x in a], [x[1] for x in a]
        self.tokens = {}
        for tok, creator, sym, cb, mf, mb, pair in con.execute(
                "SELECT token, creator, symbol, create_block, max_funds, migrate_block, pair FROM tokens WHERE quote=?",
                (ZERO,)):
            if cb is None or cb < self.start:
                continue
            self.tokens[tok] = dict(creator=creator, symbol=sym, create=cb, mig=mb, curve=Curve(mf),
                                    pm_loaded=False)
        # сделки на кривой: по токену (block, idx, account, side, amount, cost, funds)
        self.trades = defaultdict(list)
        for row in con.execute("SELECT token, block, idx, account, side, amount, cost, funds, price FROM trades "
                               "ORDER BY token, block, idx"):
            if row[0] in self.tokens:
                self.trades[row[0]].append(row[1:])
        self.tblocks = {t: [r[0] for r in v] for t, v in self.trades.items()}
        self.syncs = defaultdict(list)
        for tok, b, i, rt, rb in con.execute("SELECT token, block, idx, r_token, r_bnb FROM syncs ORDER BY token, block, idx"):
            self.syncs[tok].append((b, i, rt, rb))
        self.sblocks = {t: [r[0] for r in v] for t, v in self.syncs.items()}
        self.transfers = defaultdict(list)
        for tok, b, i, s, d, amt in con.execute("SELECT token, block, idx, src, dst, amount FROM transfers "
                                                "ORDER BY token, block, idx"):
            self.transfers[tok].append((b, i, s, d, amt))
        for tok, in con.execute("SELECT token FROM postmig_done"):
            if tok in self.tokens:
                self.tokens[tok]["pm_loaded"] = True
        # сделки по кошельку
        self.by_wallet = defaultdict(list)   # account → [(token, block, idx, side, amount, cost, funds)]
        self.by_wt = defaultdict(list)       # (account, token) → [(block, idx, delta)]
        for tok, rows in self.trades.items():
            for b, i, acc, side, amt, cost, funds, price in rows:
                self.by_wallet[acc].append((tok, b, i, side, amt, cost, funds))
                self.by_wt[(acc, tok)].append((b, i, side * amt))
        for v in self.by_wallet.values():
            v.sort(key=lambda r: (r[1], r[2]))
        self.days = max(1e-9, (self.ts(self.end) - self.ts(self.start)) / 86400)
        self.spb = (self._at[-1] - self._at[0]) / max(1, self._ab[-1] - self._ab[0])

    def blocks(self, hours):
        return int(hours * 3600 / self.spb)

    def ts(self, block):
        i = bisect.bisect_right(self._ab, block) - 1
        i = max(0, min(i, len(self._ab) - 2))
        b0, b1, t0, t1 = self._ab[i], self._ab[i + 1], self._at[i], self._at[i + 1]
        return t0 + (t1 - t0) * (block - b0) / max(1, b1 - b0)

    # --- состояние рынка на начало блока ----------------------------------
    def funds_before(self, tok, block):
        i = bisect.bisect_left(self.tblocks[tok], block) - 1
        return self.trades[tok][i][6] if i >= 0 else 0.0

    def migrated_at(self, tok, block):
        mb = self.tokens[tok]["mig"]
        return mb is not None and block > mb

    def sell_value(self, tok, block, tokens):
        """BNB за продажу tokens в начале блока block (кривая или пара)."""
        info = self.tokens[tok]
        if not self.migrated_at(tok, block):
            return info["curve"].sell(self.funds_before(tok, block), tokens)
        s = self.syncs.get(tok)
        if s:
            i = max(0, bisect.bisect_left(self.sblocks[tok], block) - 1)
            return pancake_sell(s[i][2], s[i][3], tokens)
        # данных по паре нет: оцениваем по пулу на старте (200 млн токенов + сбор кривой)
        c = info["curve"]
        r_tok = config.TOTAL_SUPPLY - config.CURVE_OFFERS
        return pancake_sell(r_tok, r_tok * c.spot(c.fm), tokens)

    def spot(self, tok, block):
        if not self.migrated_at(tok, block):
            return self.tokens[tok]["curve"].spot(self.funds_before(tok, block))
        s = self.syncs.get(tok)
        if s:
            i = max(0, bisect.bisect_left(self.sblocks[tok], block) - 1)
            return s[i][3] / s[i][2] if s[i][2] else 0.0
        c = self.tokens[tok]["curve"]
        return c.spot(c.fm)

    def market_blocks(self, tok, frm, to):
        """Блоки всех событий токена в (frm, to] — для стопа и простоя."""
        tb = self.tblocks.get(tok, [])
        out = tb[bisect.bisect_right(tb, frm):bisect.bisect_right(tb, to)]
        sb = self.sblocks.get(tok, [])
        if sb:
            out = sorted(set(out) | set(sb[bisect.bisect_right(sb, frm):bisect.bisect_right(sb, to)]))
        return out


def wallet_events(data, wallet, tok, after_block):
    """Изменения баланса кошелька в токене после первой покупки: (block, idx, delta)."""
    ev = [x for x in data.by_wt[(wallet, tok)] if (x[0], x[1]) > after_block]
    for b, i, s, d, amt in data.transfers.get(tok, []):
        if s == wallet and d != wallet:
            ev.append((b, i, -amt))
        elif d == wallet and s != wallet:
            ev.append((b, i, amt))
    ev.sort()
    return ev


def simulate_signal(data, wallet, tok, b0, i0, amt0, cost0, funds0, k):
    """Одна копия: первая покупка кошелька в блоке b0. Возвращает dict или None (пропуск)."""
    info = data.tokens[tok]
    curve = info["curve"]
    e = b0 + k
    sig = dict(wallet=wallet, token=tok, symbol=info["symbol"], k=k, signal_block=b0,
               entry_ts=data.ts(e), snipe=(b0 - info["create"]) <= config.SNIPE_BLOCKS)
    if info["creator"] == wallet:
        return dict(sig, skip="creator")
    if data.migrated_at(tok, e):
        return dict(sig, skip="migrated")
    y = max(0.0, funds0 - cost0) + curve.b      # кривая перед его покупкой
    sig["impact"] = ((y + cost0) / y) ** 2 - 1
    sig["entry_mcap_bnb"] = curve.spot(data.funds_before(tok, e)) * config.TOTAL_SUPPLY
    pos = curve.buy(data.funds_before(tok, e), config.ENTRY_BNB)
    if not pos:
        return dict(sig, skip="near_graduation")
    entry_price = config.ENTRY_BNB / pos
    stop_price = entry_price * (1 - config.STOP_LOSS)
    spent = config.ENTRY_BNB + config.GAS_BNB
    got = 0.0
    bal = amt0
    max_bal = amt0
    wallet_exit = None
    bait = False
    reason = "open"
    exit_block = None
    horizon_end = data.end
    if info["mig"] is not None and info["pm_loaded"]:
        horizon_end = min(data.end, info["mig"] + data.blocks(config.POSTMIG_HOURS))
    elif info["mig"] is not None:
        horizon_end = min(data.end, info["mig"] + 1)   # без данных DEX закрываемся на выходе

    idle_blocks = data.blocks(config.IDLE_HOURS)
    events = wallet_events(data, wallet, tok, (b0, i0))
    cur = e   # последний обработанный блок
    for (b, i, delta) in events + [(horizon_end + 1, 0, None)]:
        # стоп и простой между текущим моментом и следующим событием кошелька
        seg_end = min(b, horizon_end)
        last = cur
        for mb in data.market_blocks(tok, cur, seg_end):
            if mb - last > idle_blocks:
                exit_block, reason = last + idle_blocks, "idle"
                break
            if data.spot(tok, mb + 1) <= stop_price:
                exit_block, reason = mb + k, "stop"
                break
            last = mb
        else:
            if seg_end - last > idle_blocks:
                exit_block, reason = last + idle_blocks, "idle"
        if exit_block is not None:
            break
        if delta is None or b > horizon_end:
            break
        cur = b
        if delta > 0:                        # докупка кошелька — не копируем
            bal += delta
            max_bal = max(max_bal, bal)
            continue
        frac = min(1.0, -delta / bal) if bal > 0 else 1.0
        if b - b0 <= config.BAIT_BLOCKS and frac >= 0.5:
            bait = True
        bal = max(0.0, bal + delta)
        if bal <= max_bal * 0.01:
            frac = 1.0
        q = pos * frac
        got += data.sell_value(tok, b + k, q)
        spent += config.GAS_BNB
        pos -= q
        if pos <= 1e-9:
            reason, exit_block = "follow", b + k
            wallet_exit = b
            break
    if pos > 1e-9:
        if exit_block is None:
            if info["mig"] is not None and horizon_end < data.end:
                exit_block, reason = horizon_end, ("horizon" if info["pm_loaded"] else "migrated_nodata")
            else:
                exit_block = data.end
        got += data.sell_value(tok, exit_block, pos)
        if reason != "open":
            spent += config.GAS_BNB
    pnl = got - spent
    hold_end = wallet_exit or exit_block
    return dict(sig, skip=None, spent=spent, got=got, pnl=pnl, ret=pnl / spent, exit=reason,
                hold_sec=data.ts(exit_block) - data.ts(e), wallet_hold_sec=data.ts(hold_end) - data.ts(b0),
                bait=bait)


def first_buys(data, wallet):
    seen = set()
    for tok, b, i, side, amt, cost, funds in data.by_wallet[wallet]:
        if side == 1 and tok not in seen:
            seen.add(tok)
            yield tok, b, i, amt, cost, funds
        elif tok not in seen:
            seen.add(tok)   # первой в токене была продажа (купил до окна) — сигнала нет


def simulate_wallet(data, wallet, latencies=config.LATENCIES):
    out = []
    for tok, b, i, amt, cost, funds in first_buys(data, wallet):
        for k in latencies:
            out.append(simulate_signal(data, wallet, tok, b, i, amt, cost, funds, k))
    return out

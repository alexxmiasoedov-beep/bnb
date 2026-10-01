"""Сбор данных из блокчейна в SQLite.

Шаг 1 (collect): события TokenManager2 за окно — создания токенов, сделки на
кривой, выходы на PancakeSwap; затем валюта котировки каждого токена
(оставляем только токены за BNB).
Шаг 2 (postmig): для токенов, вышедших на PancakeSwap, — резервы пары (Sync)
и переводы токена у кошельков-кандидатов, чтобы видеть их продажи на DEX.
"""
import sys
import time

from . import config, db
from .rpc import Rpc

SEL_TOKEN_INFO = "0x1f69565f"
SEL_GET_PAIR = "0xe6a43905"
ZERO = "0x" + "0" * 40


def _words(data):
    d = data[2:]
    return [d[i:i + 64] for i in range(0, len(d), 64)]


def _addr(word):
    return "0x" + word[-40:].lower()


def _str_at(words, offset_word):
    """ABI-строка по смещению (в байтах) из заголовка."""
    try:
        i = int(offset_word, 16) // 32
        n = int(words[i], 16)
        raw = "".join(words[i + 1:i + 1 + (n + 31) // 32])[:2 * n]
        return bytes.fromhex(raw).decode("utf-8", "replace")
    except Exception:
        return ""


def _pad(addr):
    return "0x" + addr[2:].lower().rjust(64, "0")


def _progress(msg):
    sys.stderr.write("\r" + msg + " " * 10)
    sys.stderr.flush()


def ensure_anchors(con, rpc, frm, to, step=20000):
    """Время блоков: опорные точки раз в step блоков, между ними — интерполяция."""
    have = {b for (b,) in con.execute("SELECT block FROM anchors")}
    need = [b for b in list(range(frm, to, step)) + [to] if b not in have]
    if not need:
        return
    res = rpc.batch([("eth_getBlockByNumber", [hex(b), False]) for b in need])
    con.executemany("INSERT OR REPLACE INTO anchors VALUES(?,?)",
                    [(b, int(r["timestamp"], 16)) for b, r in zip(need, res) if r])
    con.commit()


def collect(days, con=None, rpc=None):
    con = con or db.connect()
    rpc = rpc or Rpc()
    head = rpc.block_number() - 5
    start = db.meta_get(con, "start_block")
    if start is None:
        spb = (rpc.block_time(head) - rpc.block_time(head - 20000)) / 20000
        start = head - int(days * 86400 / spb)
        db.meta_set(con, "start_block", start)
    start = int(start)
    frm = int(db.meta_get(con, "last_block", start - 1)) + 1
    ensure_anchors(con, rpc, start, head)

    topics = [[config.T_CREATE, config.T_BUY, config.T_SELL, config.T_LIQ]]
    t0 = time.time()
    b = frm
    while b <= head:
        e = min(b + config.LOG_CHUNK - 1, head)
        logs = rpc.logs(b, e, config.TOKEN_MANAGER, topics)
        creates, trades, migr = [], [], []
        for l in logs:
            w = _words(l["data"])
            blk, idx = int(l["blockNumber"], 16), int(l["logIndex"], 16)
            t = l["topics"][0]
            if t == config.T_CREATE:
                creates.append((_addr(w[1]), _addr(w[0]), _str_at(w, w[4]), blk))
            elif t in (config.T_BUY, config.T_SELL):
                v = [int(x, 16) / 1e18 for x in w[2:8]]  # price amount cost fee offers funds
                trades.append((blk, idx, _addr(w[0]), _addr(w[1]), 1 if t == config.T_BUY else -1,
                               v[0], v[1], v[2], v[4], v[5]))
            elif t == config.T_LIQ:
                migr.append((blk, _addr(w[0])))
        con.executemany("INSERT OR IGNORE INTO tokens(token,creator,symbol,create_block) VALUES(?,?,?,?)", creates)
        con.executemany("INSERT OR IGNORE INTO trades VALUES(?,?,?,?,?,?,?,?,?,?)", trades)
        con.executemany("UPDATE tokens SET migrate_block=? WHERE token=?", migr)
        db.meta_set(con, "last_block", e)
        done = (e - frm + 1) / max(1, head - frm + 1)
        eta = (time.time() - t0) / max(done, 1e-9) * (1 - done)
        _progress(f"блоки {e - start + 1}/{head - start + 1}  {done:.0%}  осталось ~{eta / 60:.0f} мин")
        b = e + 1
    sys.stderr.write("\n")
    fill_quotes(con, rpc)
    # сделки по токенам не за BNB и по токенам, созданным до окна, не нужны
    con.execute("""DELETE FROM trades WHERE token NOT IN
                   (SELECT token FROM tokens WHERE quote=?)""", (ZERO,))
    con.commit()
    n_tok = con.execute("SELECT COUNT(*) FROM tokens WHERE quote=?", (ZERO,)).fetchone()[0]
    n_tr = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    print(f"готово: токенов за BNB {n_tok}, сделок на кривой {n_tr}")
    return con


MULTICALL3 = "0xca11bde05977b3631167028862be2a173976ca11"


def multicall(rpc, calls):
    """Multicall3.tryAggregate(false, [(target, data)]) → [bytes hex | None]: сотни вызовов за один запрос."""
    n = len(calls)
    head, body = [], []
    off = 32 * n
    for target, data in calls:
        b = data[2:]
        blen = len(b) // 2
        padded = b + "0" * ((-len(b)) % 64)
        tup = target[2:].lower().rjust(64, "0") + hex(64)[2:].rjust(64, "0") + hex(blen)[2:].rjust(64, "0") + padded
        head.append(hex(off)[2:].rjust(64, "0"))
        body.append(tup)
        off += len(tup) // 2
    enc = "0" * 64 + hex(64)[2:].rjust(64, "0") + hex(n)[2:].rjust(64, "0") + "".join(head) + "".join(body)
    r = rpc.call("eth_call", [{"to": MULTICALL3, "data": "0xbce38bd7" + enc}, "latest"])[2:]
    arr = int(r[0:64], 16) * 2
    cnt = int(r[arr:arr + 64], 16)
    base = arr + 64
    out = []
    for i in range(cnt):
        t = base + int(r[base + 64 * i:base + 64 * i + 64], 16) * 2
        ok = int(r[t:t + 64], 16)
        d = t + int(r[t + 64:t + 128], 16) * 2
        ln = int(r[d:d + 64], 16) * 2
        out.append("0x" + r[d + 64:d + 64 + ln] if ok else None)
    return out


def fill_quotes(con, rpc, part_size=300):
    todo = [t for (t,) in con.execute("SELECT token FROM tokens WHERE quote IS NULL")]
    for i in range(0, len(todo), part_size):
        part = todo[i:i + part_size]
        res = multicall(rpc, [(config.HELPER, SEL_TOKEN_INFO + _pad(t)[2:]) for t in part])
        rows = []
        for t, r in zip(part, res):
            if not r or len(r) < 2 + 64 * 11:
                rows.append(("?", None, t))
                continue
            w = _words(r)
            rows.append((_addr(w[2]), int(w[10], 16) / 1e18, t))
        con.executemany("UPDATE tokens SET quote=?, max_funds=? WHERE token=?", rows)
        con.commit()
        _progress(f"валюта котировки: {min(i + part_size, len(todo))}/{len(todo)}")
    sys.stderr.write("\n")


def candidate_wallets(con, min_tokens):
    """Кошельки, у которых достаточно первых покупок, чтобы вообще попасть в отбор."""
    q = """SELECT account FROM trades WHERE side=1 GROUP BY account
           HAVING COUNT(DISTINCT token) >= ?"""
    return {a for (a,) in con.execute(q, (min_tokens,))}


def postmig(con=None, rpc=None):
    con = con or db.connect()
    rpc = rpc or Rpc()
    end = int(db.meta_get(con, "last_block"))
    anchors = con.execute("SELECT block, ts FROM anchors ORDER BY block").fetchall()
    spb = (anchors[-1][1] - anchors[0][1]) / max(1, anchors[-1][0] - anchors[0][0])
    horizon = int(config.POSTMIG_HOURS * 3600 / spb)
    cands = candidate_wallets(con, max(5, config.MIN_SIGNALS // 2))
    toks = con.execute("""SELECT token, migrate_block, pair FROM tokens WHERE quote=? AND migrate_block IS NOT NULL
                          AND token NOT IN (SELECT token FROM postmig_done WHERE to_block >= MIN(?, migrate_block + ?))""",
                       (ZERO, end, horizon)).fetchall()
    print(f"токенов на PancakeSwap: {len(toks)}, кошельков-кандидатов: {len(cands)}")
    for n, (tok, mb, pair) in enumerate(toks, 1):
        _progress(f"PancakeSwap: токен {n}/{len(toks)}")
        if not pair:
            r = rpc.call("eth_call", [{"to": config.PANCAKE_V2_FACTORY,
                                       "data": SEL_GET_PAIR + _pad(tok)[2:] + _pad(config.WBNB)[2:]}, "latest"])
            pair = _addr(r[2:])
            con.execute("UPDATE tokens SET pair=? WHERE token=?", (pair, tok))
        to = min(end, mb + horizon)
        if pair != ZERO:
            tok_is_0 = tok < config.WBNB
            rows = []
            for l in _chunked_logs(rpc, mb, to, pair, [config.T_SYNC]):
                w = _words(l["data"])
                r0, r1 = int(w[0], 16) / 1e18, int(w[1], 16) / 1e18
                rows.append((int(l["blockNumber"], 16), int(l["logIndex"], 16), tok,
                             r0 if tok_is_0 else r1, r1 if tok_is_0 else r0))
            con.executemany("INSERT OR IGNORE INTO syncs VALUES(?,?,?,?,?)", rows)
        # держатели-кандидаты на момент выхода
        holders = [a for (a, bal) in con.execute(
            "SELECT account, SUM(side*amount) FROM trades WHERE token=? AND block<=? GROUP BY account",
            (tok, mb)) if a in cands and bal > 1]
        rows = []
        for i in range(0, len(holders), 100):
            padded = [_pad(a) for a in holders[i:i + 100]]
            for tp in ([config.T_TRANSFER, padded], [config.T_TRANSFER, None, padded]):
                for l in _chunked_logs(rpc, mb, to, tok, tp):
                    rows.append((int(l["blockNumber"], 16), int(l["logIndex"], 16), tok,
                                 _addr(l["topics"][1]), _addr(l["topics"][2]), int(l["data"], 16) / 1e18))
        con.executemany("INSERT OR IGNORE INTO transfers VALUES(?,?,?,?,?,?)", rows)
        con.execute("INSERT OR REPLACE INTO postmig_done VALUES(?,?)", (tok, to))
        con.commit()
    sys.stderr.write("\n")
    return con


def _chunked_logs(rpc, frm, to, address, topics, chunk=50000):
    out = []
    b = frm
    while b <= to:
        e = min(b + chunk - 1, to)
        out += rpc.logs(b, e, address, topics)
        b = e + 1
    return out

"""python -m fmcopy collect --days 14   — собрать данные
python -m fmcopy postmig              — догрузить данные PancakeSwap для вышедших токенов
python -m fmcopy select               — симуляция, отбор, отчёт в reports/
python -m fmcopy wallet 0x...         — разбор одного кошелька
"""
import argparse
from datetime import datetime, timezone

from . import collect, config, ranking
from .sim import Data, simulate_wallet


def wallet_report(addr):
    addr = addr.lower()
    data = Data()
    rows = data.by_wallet.get(addr)
    if not rows:
        print("по кошельку нет сделок в собранном окне")
        return
    # собственный результат кошелька по токенам
    own = {}
    for tok, b, i, side, amt, cost, funds in rows:
        o = own.setdefault(tok, dict(first=b, spent=0.0, got=0.0, bal=0.0, mcap=None))
        if side == 1:
            o["spent"] += cost * (1 + config.TRADE_FEE)
            o["bal"] += amt
            if o["mcap"] is None:
                o["mcap"] = data.tokens[tok]["curve"].spot(funds) * config.TOTAL_SUPPLY
        else:
            o["got"] += cost * (1 - config.TRADE_FEE)
            o["bal"] -= amt
    copy = {r["token"]: r for r in simulate_wallet(data, addr, (config.MAIN_LATENCY,))}
    print(f"{'дата UTC':12} {'токен':12} {'капа входа':>10} {'вложил':>8} {'вывел':>8} {'PnL его':>9} "
          f"{'копия PnL':>10}  выход копии")
    tot_own = tot_copy = 0.0
    for tok, o in sorted(own.items(), key=lambda x: x[1]["first"]):
        if o["bal"] > 1:
            o["got"] += data.sell_value(tok, data.end, o["bal"])
        pnl = o["got"] - o["spent"]
        tot_own += pnl
        c = copy.get(tok)
        cp = f"{c['pnl']:+10.4f}" if c and c["skip"] is None else f"{'—':>10}"
        if c and c["skip"] is None:
            tot_copy += c["pnl"]
        why = (c["exit"] if c["skip"] is None else "пропуск: " + c["skip"]) if c else "нет первой покупки"
        dt = datetime.fromtimestamp(data.ts(o["first"]), timezone.utc).strftime("%d.%m %H:%M")
        sym = (data.tokens[tok]["symbol"] or tok[:10])[:12]
        mc = f"{o['mcap']:10.1f}" if o["mcap"] else f"{'—':>10}"
        print(f"{dt:12} {sym:12} {mc} {o['spent']:8.3f} {o['got']:8.3f} {pnl:+9.4f} {cp}  {why}")
    print(f"\nитого: PnL кошелька {tot_own:+.3f} BNB, PnL копирования по 0,1 BNB {tot_copy:+.3f} BNB "
          f"(капа — в BNB, открытые позиции оценены по цене продажи на конец окна)")


def main():
    p = argparse.ArgumentParser(prog="fmcopy")
    sp = p.add_subparsers(dest="cmd", required=True)
    c = sp.add_parser("collect")
    c.add_argument("--days", type=float, default=14)
    sp.add_parser("postmig")
    s = sp.add_parser("select")
    s.add_argument("--min-signals", type=int)
    w = sp.add_parser("wallet")
    w.add_argument("address")
    a = p.parse_args()
    if a.cmd == "collect":
        collect.collect(a.days)
    elif a.cmd == "postmig":
        collect.postmig()
    elif a.cmd == "select":
        ranking.run(a.min_signals)
    elif a.cmd == "wallet":
        wallet_report(a.address)


if __name__ == "__main__":
    main()

"""Метрики, фильтры, рейтинг кошельков и проверка портфеля на отложенном периоде."""
import csv
import math
import os
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

from . import config
from .sim import Data, simulate_wallet

FIELDS = ["wallet", "rank", "status", "score", "n", "pnl_bnb", "roi", "expectancy", "win_rate", "profit_factor",
          "pnl_ex_top2", "pos_weeks", "max_dd_bnb", "pnl_k1", "pnl_k3", "test_n", "test_pnl", "test_expectancy",
          "snipe_share", "creator_share", "trades_per_day", "median_hold_sec", "big_impact_share", "bait_share",
          "median_entry_mcap_bnb", "skipped", "reasons"]


def metrics(rows):
    """rows — результаты симуляции (без пропусков) одного кошелька при одной задержке."""
    n = len(rows)
    if not n:
        return dict(n=0, pnl_bnb=0.0, expectancy=0.0, roi=0.0, win_rate=0.0, profit_factor=0.0,
                    pnl_ex_top2=0.0, pos_weeks=0.0, max_dd_bnb=0.0, tstat=0.0)
    pnls = [r["pnl"] for r in rows]
    rets = [r["ret"] for r in rows]
    gain = sum(p for p in pnls if p > 0)
    loss = -sum(p for p in pnls if p < 0)
    weeks = defaultdict(list)
    for r in rows:
        weeks[datetime.fromtimestamp(r["entry_ts"], timezone.utc).isocalendar()[:2]].append(r["pnl"])
    full_weeks = [sum(v) for v in weeks.values() if len(v) >= 3]
    cum = peak = dd = 0.0
    for r in sorted(rows, key=lambda r: r["entry_ts"]):
        cum += r["pnl"]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    sd = statistics.pstdev(rets) if n > 1 else 0.0
    return dict(
        n=n, pnl_bnb=sum(pnls), expectancy=sum(pnls) / n, roi=sum(pnls) / sum(r["spent"] for r in rows),
        win_rate=sum(p > 0 for p in pnls) / n,
        profit_factor=gain / loss if loss else (math.inf if gain else 0.0),
        pnl_ex_top2=sum(sorted(pnls)[:-2]) if n > 2 else min(0.0, sum(pnls)),
        pos_weeks=(sum(w > 0 for w in full_weeks) / len(full_weeks)) if full_weeks else 0.0,
        max_dd_bnb=dd, tstat=(statistics.mean(rets) / sd * math.sqrt(n)) if sd > 0 else 0.0)


def evaluate(data, wallet, split_ts):
    res = simulate_wallet(data, wallet)
    by_k = defaultdict(list)
    for r in res:
        by_k[r["k"]].append(r)
    main = by_k[config.MAIN_LATENCY]
    ok = [r for r in main if r["skip"] is None]
    train = [r for r in ok if r["entry_ts"] < split_ts]
    test = [r for r in ok if r["entry_ts"] >= split_ts]
    m = metrics(train)
    mt = metrics(test)
    allk = {k: metrics([r for r in v if r["skip"] is None and r["entry_ts"] < split_ts]) for k, v in by_k.items()}
    sig = [r for r in main if r["entry_ts"] < split_ts]
    trades = data.by_wallet[wallet]
    holds = [r["wallet_hold_sec"] for r in train]
    out = dict(
        wallet=wallet, score=m["tstat"], **{k: m[k] for k in ("n", "pnl_bnb", "roi", "expectancy", "win_rate",
                                                              "profit_factor", "pnl_ex_top2", "pos_weeks",
                                                              "max_dd_bnb")},
        pnl_k1=allk.get(1, {}).get("pnl_bnb", 0.0), pnl_k3=allk.get(3, {}).get("pnl_bnb", 0.0),
        test_n=mt["n"], test_pnl=mt["pnl_bnb"], test_expectancy=mt["expectancy"],
        snipe_share=_share(sig, lambda r: r["snipe"]),
        creator_share=_share(sig, lambda r: r["skip"] == "creator"),
        trades_per_day=len(trades) / data.days,
        median_hold_sec=statistics.median(holds) if holds else 0.0,
        big_impact_share=_share(train, lambda r: r.get("impact", 0) > config.BIG_IMPACT),
        bait_share=_share(train, lambda r: r["bait"]),
        median_entry_mcap_bnb=statistics.median([r["entry_mcap_bnb"] for r in train]) if train else 0.0,
        skipped=sum(1 for r in sig if r["skip"]))
    return out, main


def _share(rows, pred):
    return sum(1 for r in rows if pred(r)) / len(rows) if rows else 0.0


def reject_reasons(w):
    c = config
    rules = [
        (w["n"] < c.MIN_SIGNALS, f"мало сделок ({w['n']})"),
        (w["creator_share"] > c.MAX_CREATOR_SHARE, "создатель токенов"),
        (w["snipe_share"] > c.MAX_SNIPE_SHARE, "снайпер"),
        (w["trades_per_day"] > c.MAX_TRADES_PER_DAY, "бот: слишком много сделок"),
        (w["median_hold_sec"] < c.MIN_MEDIAN_HOLD_SEC, "держит слишком мало"),
        (w["big_impact_share"] > c.MAX_BIG_IMPACT_SHARE, "двигает цену своими покупками"),
        (w["bait_share"] > c.MAX_BAIT_SHARE, "приманка для копировщиков"),
        (w["expectancy"] <= 0, "минус при копировании"),
        (w["profit_factor"] < c.MIN_PROFIT_FACTOR, "профит-фактор < %.1f" % c.MIN_PROFIT_FACTOR),
        (w["pnl_ex_top2"] <= 0, "прибыль только от 1–2 сделок"),
        (w["pos_weeks"] < c.MIN_POS_WEEKS, "нестабилен по неделям"),
        (w["pnl_k3"] <= 0, "не выдерживает задержку 3 блока"),
    ]
    return [msg for bad, msg in rules if bad]


def clusters(data, wallets):
    """Кошельки, которые слишком часто входят в один токен в одном блоке, — один игрок."""
    seen = defaultdict(set)
    for w in wallets:
        got = set()
        for tok, b, i, side, amt, cost, funds in data.by_wallet[w]:
            if side == 1 and tok not in got:
                got.add(tok)
                seen[(tok, b)].add(w)
    pair = defaultdict(int)
    for ws in seen.values():
        ws = sorted(ws)
        for a in range(len(ws)):
            for b in range(a + 1, len(ws)):
                pair[(ws[a], ws[b])] += 1
    parent = {w: w for w in wallets}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for (a, b), n in pair.items():
        if n >= config.CLUSTER_MIN_COMMON:
            parent[find(a)] = find(b)
    return {w: find(w) for w in wallets}


def portfolio(signals_by_wallet, wallets, split_ts):
    """Портфель на проверочном периоде: один токен — одна позиция, за первым вошедшим кошельком."""
    sig = [r for w in wallets for r in signals_by_wallet[w] if r["skip"] is None and r["entry_ts"] >= split_ts]
    sig.sort(key=lambda r: r["entry_ts"])
    taken, out = set(), []
    for r in sig:
        if r["token"] in taken:
            continue
        taken.add(r["token"])
        out.append(r)
    return out


def run(min_signals=None, out_dir="reports"):
    if min_signals is not None:
        config.MIN_SIGNALS = min_signals
    t0 = time.time()
    data = Data()
    split_ts = data.ts(data.start) + (data.ts(data.end) - data.ts(data.start)) * (1 - config.TEST_SHARE)
    # кандидаты: достаточно первых покупок за обучающий период
    cands = []
    for w, rows in data.by_wallet.items():
        firsts = {}
        for tok, b, *_ in rows:
            firsts.setdefault(tok, b)
        if sum(1 for b in firsts.values() if data.ts(b) < split_ts) >= config.MIN_SIGNALS:
            cands.append(w)
    print(f"окно {data.days:.1f} дн., токенов за BNB {len(data.tokens)}, кошельков {len(data.by_wallet)}, "
          f"кандидатов (≥{config.MIN_SIGNALS} входов) {len(cands)}")
    results, signals = [], {}
    for n, w in enumerate(cands, 1):
        r, main = evaluate(data, w, split_ts)
        r["reasons"] = "; ".join(reject_reasons(r))
        results.append(r)
        signals[w] = main
        if n % 20 == 0 or n == len(cands):
            sys.stderr.write(f"\rсимуляция: {n}/{len(cands)}")
    sys.stderr.write("\n")

    passed = [r for r in results if not r["reasons"]]
    cl = clusters(data, [r["wallet"] for r in passed])
    best_in_cluster = {}
    for r in sorted(passed, key=lambda r: -r["score"]):
        best_in_cluster.setdefault(cl[r["wallet"]], r["wallet"])
    for r in results:
        if r["reasons"]:
            r["status"] = "отсеян"
        elif best_in_cluster[cl[r["wallet"]]] != r["wallet"]:
            r["status"] = "дубль кластера"
        elif r["test_n"] < 5:
            r["status"] = "мало данных на проверке"
        elif r["test_expectancy"] <= 0:
            r["status"] = "не подтвердился на проверке"
        else:
            r["status"] = "ОТОБРАН"
    results.sort(key=lambda r: (r["status"] != "ОТОБРАН", r["reasons"] != "", -r["score"]))
    for i, r in enumerate(results, 1):
        r["rank"] = i
    chosen = [r["wallet"] for r in results if r["status"] == "ОТОБРАН"][:config.PORTFOLIO_SIZE]
    port = portfolio(signals, chosen, split_ts)

    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "wallets.csv"), "w", newline="") as f:
        wr = csv.DictWriter(f, FIELDS, extrasaction="ignore")
        wr.writeheader()
        for r in results:
            wr.writerow({k: (round(v, 6) if isinstance(v, float) else v) for k, v in r.items()})
    with open(os.path.join(out_dir, "portfolio_trades.csv"), "w", newline="") as f:
        cols = ["wallet", "token", "symbol", "entry_ts", "entry_mcap_bnb", "spent", "got", "pnl", "ret", "exit",
                "hold_sec"]
        wr = csv.DictWriter(f, cols, extrasaction="ignore")
        wr.writeheader()
        for r in port:
            wr.writerow(dict(r, entry_ts=datetime.fromtimestamp(r["entry_ts"], timezone.utc).isoformat()))
    pm = metrics(port)
    write_report(os.path.join(out_dir, "report.md"), data, results, chosen, pm, split_ts)
    print(f"отобрано {len(chosen)} кошельков; портфель на проверке: {pm['n']} сделок, "
          f"PnL {pm['pnl_bnb']:+.3f} BNB, ROI {pm['roi']:+.1%}  ({time.time() - t0:.0f} с)")
    print(f"отчёт: {out_dir}/report.md, {out_dir}/wallets.csv, {out_dir}/portfolio_trades.csv")


def write_report(path, data, results, chosen, pm, split_ts):
    d = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%d.%m %H:%M")
    rej = defaultdict(int)
    for r in results:
        for x in filter(None, r["reasons"].split("; ")):
            rej[x.split(" (")[0]] += 1
    L = [f"# Отбор кошельков Four.meme для копирования",
         "",
         f"Окно: {d(data.ts(data.start))} — {d(data.ts(data.end))} UTC ({data.days:.1f} дн.), "
         f"обучение до {d(split_ts)}, дальше проверка.",
         f"Вход {config.ENTRY_BNB} BNB, задержка копии {config.MAIN_LATENCY} блока, стоп −{config.STOP_LOSS:.0%}, "
         f"выход при простое {config.IDLE_HOURS:g} ч.",
         "",
         f"Кандидатов: {len(results)}. Отобрано: **{len(chosen)}**.",
         "",
         "## Портфель на проверочном периоде",
         "",
         f"Сделок {pm['n']}, PnL **{pm['pnl_bnb']:+.3f} BNB**, ROI {pm['roi']:+.1%}, "
         f"винрейт {pm['win_rate']:.0%}, профит-фактор {pm['profit_factor']:.2f}, "
         f"макс. просадка {pm['max_dd_bnb']:.3f} BNB.",
         "",
         "## Отобранные кошельки",
         "",
         "| # | Кошелёк | Сделок | PnL, BNB | Ожидание | Винрейт | PF | Без топ-2 | k=3 | Проверка: n / PnL | Капа входа, BNB |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        if r["wallet"] not in chosen:
            continue
        L.append(f"| {r['rank']} | `{r['wallet']}` | {r['n']} | {r['pnl_bnb']:+.3f} | {r['expectancy']:+.4f} | "
                 f"{r['win_rate']:.0%} | {r['profit_factor']:.2f} | {r['pnl_ex_top2']:+.3f} | {r['pnl_k3']:+.3f} | "
                 f"{r['test_n']} / {r['test_pnl']:+.3f} | {r['median_entry_mcap_bnb']:.1f} |")
    L += ["", "## Почему отсеяны остальные", "", "| Причина | Кошельков |", "|---|---|"]
    L += [f"| {k} | {v} |" for k, v in sorted(rej.items(), key=lambda x: -x[1])]
    L += ["", "Полная таблица со всеми метриками — `wallets.csv`, сделки портфеля — `portfolio_trades.csv`."]
    with open(path, "w") as f:
        f.write("\n".join(L) + "\n")

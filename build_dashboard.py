"""
Автообновляемый дашборд «PastiLab: Wildberries + Ozon».

Источники:
  - WB Statistics API, reportDetailByPeriod (4 кабинета, токены в wb_export/config.yaml)
  - Ozon Seller API, /v3/finance/transaction/totals (5 кабинетов, ozon_export/config.yaml)

Окно дашборда — скользящие 13 календарных месяцев (12 полных + текущий).
Закрытые месяцы кэшируются в cache/ и не перезапрашиваются; при первом запуске
кэш WB засевается из годовых CSV-выгрузок wb_export/output/*report_detail*.csv.

Запуск:
    py -3.12 build_dashboard.py             # обновить данные и перерендерить HTML
    py -3.12 build_dashboard.py --offline   # только рендер из кэша (без API)

Выход:
    output/dashboard.html  — полный standalone-файл (можно открыть локально)
    output/artifact.html   — тот же контент без <html>/<head>/<body> (для публикации)
"""

import argparse
import calendar
import csv
import datetime as dt
import json
import logging
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(BASE, "wb_export"))
sys.path.insert(0, os.path.join(BASE, "ozon_export"))

CACHE = os.path.join(HERE, "cache")
OUT = os.path.join(HERE, "output")
# в облаке (GitHub Actions) пути к конфигам передаются через переменные окружения
WB_CFG = os.environ.get("WB_CONFIG_PATH",
                        os.path.join(BASE, "wb_export", "config.yaml"))
OZ_CFG = os.environ.get("OZON_CONFIG_PATH",
                        os.path.join(BASE, "ozon_export", "config.yaml"))
WB_CSV_DIR = os.path.join(BASE, "wb_export", "output")

# месяц считается закрытым, если после его конца прошло >= FINAL_LAG дней
FINAL_LAG = 10

log = logging.getLogger("dash")

RU_MON = ["янв", "фев", "мар", "апр", "май", "июн",
          "июл", "авг", "сен", "окт", "ноя", "дек"]

OZ_KEYS = ["accruals_for_sale", "sale_commission", "processing_and_delivery",
           "refunds_and_cancellations", "services_amount", "compensation_amount",
           "money_transfer", "others_amount"]

WB_LABELS = {"pastilab": "PastiLab", "tdbio": "ТД БИО",
             "kuznetsova": "ИП Кузнецова", "kazakova": "ИП Казакова"}
WB_ORDER = ["pastilab", "tdbio", "kuznetsova", "kazakova"]
WB_COLORS = {"pastilab": "#5b8def", "tdbio": "#f0a63c",
             "kuznetsova": "#a78bfa", "kazakova": "#3ec6a0"}

# кабинет Ozon -> категория товара
OZ_CATEGORY = {"ОЗОН Фреш пастила": "Пастила", "биг ОЗОН пастила": "Пастила",
               "биг ОЗОН пасты и урбечи": "Пасты и урбечи",
               "биг ОЗОН урбечи": "Пасты и урбечи",
               "биг ОЗОН батончики": "Батончики"}


# ----------------------- утилиты -----------------------
def num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def month_start(d: dt.date) -> dt.date:
    return d.replace(day=1)


def month_end(d: dt.date) -> dt.date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def add_months(d: dt.date, n: int) -> dt.date:
    m = d.month - 1 + n
    return dt.date(d.year + m // 12, m % 12 + 1, 1)


def window_months(today: dt.date) -> list[str]:
    """13 месяцев: текущий и 12 предыдущих, как 'YYYY-MM'."""
    first = add_months(month_start(today), -12)
    return [add_months(first, i).strftime("%Y-%m") for i in range(13)]


def is_final(month: str, today: dt.date) -> bool:
    me = month_end(dt.date.fromisoformat(month + "-01"))
    return (today - me).days >= FINAL_LAG


def mon_label(month: str, i: int, months: list[str], today: dt.date) -> str:
    y, m = int(month[:4]), int(month[5:7])
    lab = RU_MON[m - 1]
    if i == 0 or m == 1:
        lab += f"'{y % 100}"
    if month == today.strftime("%Y-%m"):
        lab += "*"
    return lab


def cache_path(kind: str, key: str, month: str) -> str:
    return os.path.join(CACHE, f"{kind}_{key}_{month}.json")


def cache_read(kind: str, key: str, month: str):
    p = cache_path(kind, key, month)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return None


def cache_write(kind: str, key: str, month: str, obj: dict):
    p = cache_path(kind, key, month)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


# ----------------------- WB: агрегация строк отчёта -----------------------
def ded_bucket(b):
    b = (b or "").lower()
    if "продвижен" in b: return "Внутренняя реклама"
    if "отзыв" in b:     return "Отзывы (баллы/выкуп)"
    if "джем" in b:      return "Подписка «Джем»"
    if "транзит" in b:   return "Транзитные поставки"
    if "утилиз" in b:    return "Утилизация"
    return "Прочие удержания"


def new_wb_agg():
    return {"retail": 0.0, "for_pay": 0.0, "delivery": 0.0, "storage": 0.0,
            "penalty": 0.0, "acceptance": 0.0, "add_pay": 0.0, "deduction": 0.0,
            "qty": 0.0}


def wb_add_row(agg, ded, sku, r):
    agg["retail"] += num(r.get("retail_amount"))
    agg["for_pay"] += num(r.get("ppvz_for_pay"))
    agg["delivery"] += num(r.get("delivery_rub"))
    agg["storage"] += num(r.get("storage_fee"))
    agg["penalty"] += num(r.get("penalty"))
    agg["acceptance"] += num(r.get("acceptance"))
    agg["add_pay"] += num(r.get("additional_payment"))
    d = num(r.get("deduction"))
    if d:
        agg["deduction"] += d
        ded[ded_bucket(r.get("bonus_type_name"))] = \
            ded.get(ded_bucket(r.get("bonus_type_name")), 0.0) + d
    oper = r.get("supplier_oper_name") or ""
    nm = str(r.get("nm_id") or r.get("sa_name") or "")
    if nm:
        s = sku.setdefault(nm, {"sa": "", "subj": "", "for_pay": 0.0,
                                "delivery": 0.0, "qty": 0.0})
        s["delivery"] += num(r.get("delivery_rub"))
        if oper == "Продажа":
            s["for_pay"] += num(r.get("ppvz_for_pay"))
            s["qty"] += num(r.get("quantity"))
            s["sa"] = r.get("sa_name") or s["sa"]
            s["subj"] = r.get("subject_name") or s["subj"]
            agg["qty"] += num(r.get("quantity"))


def wb_net(a) -> float:
    return (a["for_pay"] - a["delivery"] - a["storage"] - a["penalty"]
            - a["acceptance"] - a["deduction"] + a["add_pay"])


def wb_seed_from_csv(slug: str, today: dt.date):
    """Разово наполняет кэш закрытых месяцев из годовой CSV-выгрузки."""
    if not os.path.isdir(WB_CSV_DIR):
        return
    prefix = "wb_report_detail_" if slug == "pastilab" else f"wb_{slug}_report_detail_"
    files = [f for f in os.listdir(WB_CSV_DIR)
             if f.startswith(prefix) and f.endswith(".csv") and "копия" not in f]
    if not files:
        return
    path = os.path.join(WB_CSV_DIR, sorted(files)[-1])
    # CSV покрывает 2025-07-14..2026-07-14: полные месяцы внутри — 2025-08..2026-06
    name = os.path.basename(path).replace(prefix, "").replace(".csv", "")
    d_from, d_to = name.split("_")
    lo = month_start(add_months(dt.date.fromisoformat(d_from), 1))
    hi = month_start(dt.date.fromisoformat(d_to)) - dt.timedelta(days=1)
    full_months = set()
    m = lo
    while m <= hi:
        full_months.add(m.strftime("%Y-%m"))
        m = add_months(m, 1)
    need = [m for m in full_months if cache_read("wb", slug, m) is None]
    if not need:
        return
    log.info("[wb:%s] сею кэш из %s (%s мес.)", slug, os.path.basename(path), len(need))
    months = defaultdict(lambda: (new_wb_agg(), {}, {}))
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            mkey = (r.get("rr_dt") or r.get("sale_dt") or "")[:7]
            if mkey in full_months:
                agg, ded, sku = months[mkey]
                wb_add_row(agg, ded, sku, r)
    for mkey in need:
        agg, ded, sku = months.get(mkey, (new_wb_agg(), {}, {}))
        cache_write("wb", slug, mkey, {"final": True, "agg": agg,
                                       "ded": ded, "sku": sku})


def wb_fetch_month(client, slug: str, month: str, today: dt.date):
    a = dt.date.fromisoformat(month + "-01")
    b = min(month_end(a), today)
    rows = client.report_detail(a.isoformat(), b.isoformat())
    agg, ded, sku = new_wb_agg(), {}, {}
    dropped = 0
    for r in rows:
        mkey = (r.get("rr_dt") or r.get("sale_dt") or "")[:7]
        if mkey and mkey != month:
            dropped += 1
            continue
        wb_add_row(agg, ded, sku, r)
    if dropped:
        log.info("[wb:%s] %s: %s строк вне месяца пропущено", slug, month, dropped)
    cache_write("wb", slug, month, {"final": is_final(month, today),
                                    "agg": agg, "ded": ded, "sku": sku})
    return {"final": is_final(month, today), "agg": agg, "ded": ded, "sku": sku}


def load_wb(months: list[str], today: dt.date, offline: bool, warnings: list[str]):
    import yaml
    from wb_client import WBStatClient
    with open(WB_CFG, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cabs = {c["slug"]: c for c in cfg["cabinets"] if c.get("marketplace") == "wb"}
    data = {}   # slug -> month -> record
    for slug in WB_ORDER:
        cab = cabs.get(slug)
        if not cab:
            continue
        wb_seed_from_csv(slug, today)
        client = None
        data[slug] = {}
        for m in months:
            rec = cache_read("wb", slug, m)
            if rec is not None and rec.get("final"):
                data[slug][m] = rec
                continue
            if offline:
                if rec is not None:
                    data[slug][m] = rec
                else:
                    warnings.append(f"WB {WB_LABELS[slug]}: нет данных за {m} (offline)")
                    data[slug][m] = {"agg": new_wb_agg(), "ded": {}, "sku": {}}
                continue
            if client is None:
                client = WBStatClient(cab["name"], cab["token"])
            try:
                log.info("[wb:%s] обновляю месяц %s из API...", slug, m)
                data[slug][m] = wb_fetch_month(client, slug, m, today)
            except Exception as e:
                log.warning("[wb:%s] %s: API недоступен (%s)", slug, m, e)
                if rec is not None:
                    data[slug][m] = rec
                    warnings.append(f"WB {WB_LABELS[slug]}: {m} из устаревшего кэша")
                else:
                    data[slug][m] = {"agg": new_wb_agg(), "ded": {}, "sku": {}}
                    warnings.append(f"WB {WB_LABELS[slug]}: нет данных за {m}")
    return data


# ----------------------- Ozon -----------------------
def load_ozon(months: list[str], today: dt.date, offline: bool, warnings: list[str]):
    import yaml
    from ozon_client import OzonSellerClient
    with open(OZ_CFG, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    offset = cfg.get("timezone_offset", "+03:00")
    prev_months = [add_months(dt.date.fromisoformat(m + "-01"), -12).strftime("%Y-%m")
                   for m in months]
    data = {}   # name -> month -> totals dict
    for cab in cfg["cabinets"]:
        if cab.get("marketplace", "ozon") != "ozon":
            continue
        name, cid = cab["name"], str(cab["client_id"])
        client = None
        data[name] = {}
        for m in months + prev_months:
            rec = cache_read("ozon", cid, m)
            if rec is not None and rec.get("final"):
                data[name][m] = rec["totals"]
                continue
            if offline:
                data[name][m] = rec["totals"] if rec else {}
                if rec is None:
                    warnings.append(f"Ozon {name}: нет данных за {m} (offline)")
                continue
            if client is None:
                client = OzonSellerClient(name, cab["client_id"], cab["api_key"])
            a = dt.date.fromisoformat(m + "-01")
            b = min(month_end(a), today)
            try:
                log.info("[ozon:%s] месяц %s ...", name, m)
                tot = client.finance_totals(f"{a.isoformat()}T00:00:00.000{offset}",
                                            f"{b.isoformat()}T23:59:59.999{offset}")
                data[name][m] = tot
                cache_write("ozon", cid, m, {"final": is_final(m, today), "totals": tot})
            except Exception as e:
                log.warning("[ozon:%s] %s: API недоступен (%s)", name, m, e)
                data[name][m] = rec["totals"] if rec else {}
                warnings.append(f"Ozon {name}: {m} — {'кэш' if rec else 'нет данных'}")
    return data


def oz_val(tot: dict, key: str) -> float:
    return num(tot.get(key))


def oz_net(tot: dict) -> float:
    return sum(num(tot.get(k)) for k in OZ_KEYS)


# ----------------------- подготовка данных дашборда -----------------------
def build_model(months, today, wb, oz):
    M = {}
    # --- WB по кабинетам ---
    wb_cab = {}
    for slug in wb:
        ann = new_wb_agg()
        ded_ann = defaultdict(float)
        monthly = {}
        for m in months:
            rec = wb[slug][m]
            a = rec["agg"]
            for k in ann:
                ann[k] += a.get(k, 0.0)
            for b, v in rec.get("ded", {}).items():
                ded_ann[b] += v
            monthly[m] = a
        wb_cab[slug] = {"ann": ann, "net": sum(wb_net(wb[slug][m]["agg"]) for m in months),
                        "ded": dict(ded_ann), "monthly": monthly}
    wb_retail = sum(c["ann"]["retail"] for c in wb_cab.values())
    wb_netto = sum(c["net"] for c in wb_cab.values())
    wb_delivery = sum(c["ann"]["delivery"] for c in wb_cab.values())
    wb_ded_total = sum(c["ann"]["deduction"] for c in wb_cab.values())
    wb_storage = sum(c["ann"]["storage"] for c in wb_cab.values())
    wb_adv = sum(c["ded"].get("Внутренняя реклама", 0.0) for c in wb_cab.values())
    wb_qty = sum(c["ann"]["qty"] for c in wb_cab.values())

    # --- Ozon по кабинетам ---
    oz_cab = {}
    for name, by_m in oz.items():
        cur = {k: 0.0 for k in OZ_KEYS}
        prev = {k: 0.0 for k in OZ_KEYS}
        for m in months:
            pm = add_months(dt.date.fromisoformat(m + "-01"), -12).strftime("%Y-%m")
            for k in OZ_KEYS:
                cur[k] += oz_val(by_m.get(m, {}), k)
                prev[k] += oz_val(by_m.get(pm, {}), k)
        oz_cab[name] = {"cur": cur, "prev": prev,
                        "net": sum(cur.values()), "net_prev": sum(prev.values())}
    oz_rev = sum(c["cur"]["accruals_for_sale"] for c in oz_cab.values())
    oz_rev_prev = sum(c["prev"]["accruals_for_sale"] for c in oz_cab.values())
    oz_netto = sum(c["net"] for c in oz_cab.values())
    oz_comm = -sum(c["cur"]["sale_commission"] for c in oz_cab.values())
    oz_log = -sum(c["cur"]["processing_and_delivery"] for c in oz_cab.values())
    oz_serv = -sum(c["cur"]["services_amount"] for c in oz_cab.values())
    oz_ref = -sum(c["cur"]["refunds_and_cancellations"] for c in oz_cab.values())
    oz_other = oz_rev - oz_netto - oz_comm - oz_log - oz_serv

    # помесячная выручка Ozon: текущий год vs прошлый
    oz_m_cur, oz_m_prev = [], []
    for m in months:
        pm = add_months(dt.date.fromisoformat(m + "-01"), -12).strftime("%Y-%m")
        oz_m_cur.append(sum(oz_val(oz[n].get(m, {}), "accruals_for_sale") for n in oz))
        oz_m_prev.append(sum(oz_val(oz[n].get(pm, {}), "accruals_for_sale") for n in oz))

    # помесячно WB: розница по кабинетам + тренд логистика/маржа группы
    wb_m = {slug: [wb_cab[slug]["monthly"][m]["retail"] for m in months] for slug in wb_cab}
    trend_log, trend_margin = [], []
    for m in months:
        ret = sum(wb[slug][m]["agg"]["retail"] for slug in wb)
        dl = sum(wb[slug][m]["agg"]["delivery"] for slug in wb)
        nt = sum(wb_net(wb[slug][m]["agg"]) for slug in wb)
        trend_log.append(round(dl / ret * 100, 1) if ret else 0)
        trend_margin.append(round(nt / ret * 100, 1) if ret else 0)

    # удержания WB: группа по корзинам + таблица по кабинетам
    ded_group = defaultdict(float)
    for c in wb_cab.values():
        for b, v in c["ded"].items():
            ded_group[b] += v

    # топ SKU PastiLab
    sku_agg = {}
    for m in months:
        for k, s in wb["pastilab"][m].get("sku", {}).items():
            t = sku_agg.setdefault(k, {"sa": "", "subj": "", "for_pay": 0.0,
                                       "delivery": 0.0, "qty": 0.0})
            t["for_pay"] += s.get("for_pay", 0.0)
            t["delivery"] += s.get("delivery", 0.0)
            t["qty"] += s.get("qty", 0.0)
            t["sa"] = s.get("sa") or t["sa"]
            t["subj"] = s.get("subj") or t["subj"]
    top_sku = sorted(sku_agg.values(), key=lambda s: -s["for_pay"])[:10]

    M.update(locals())
    return M


# ----------------------- рендер -----------------------
def fr(v):        # 1 234 567
    return f"{v:,.0f}".replace(",", " ")


def mln(v, dec=1):
    return f"{v / 1e6:.{dec}f}".replace(".", ",")


def pct(v, dec=1):
    return f"{v * 100:.{dec}f}".replace(".", ",") + "%"


def tys(v):     # 206123 -> "206,1" (тыс.)
    return f"{v / 1e3:.1f}".replace(".", ",")


def render(M, months, today, warnings):
    labels = [mon_label(m, i, months, today) for i, m in enumerate(months)]
    period = (f"{dt.date.fromisoformat(months[0] + '-01').strftime('%d.%m.%Y')} — "
              f"{today.strftime('%d.%m.%Y')}")
    upd = dt.datetime.now().strftime("%d.%m.%Y %H:%M")

    wb_cab, oz_cab = M["wb_cab"], M["oz_cab"]
    wb_retail, wb_netto = M["wb_retail"], M["wb_netto"]
    oz_rev, oz_netto = M["oz_rev"], M["oz_netto"]
    total_rev = wb_retail + oz_rev
    total_net = wb_netto + oz_netto
    oz_growth = ((oz_rev - M["oz_rev_prev"]) / M["oz_rev_prev"]) if M["oz_rev_prev"] else 0

    wb_margin = wb_netto / wb_retail if wb_retail else 0
    oz_margin = oz_netto / oz_rev if oz_rev else 0
    wb_log_share = M["wb_delivery"] / wb_retail if wb_retail else 0
    oz_log_share = M["oz_log"] / oz_rev if oz_rev else 0
    oz_comm_share = M["oz_comm"] / oz_rev if oz_rev else 0
    oz_serv_share = M["oz_serv"] / oz_rev if oz_rev else 0
    oz_ref_share = M["oz_ref"] / oz_rev if oz_rev else 0
    wb_advst_share = (M["wb_adv"] + M["wb_storage"]) / wb_retail if wb_retail else 0

    # ---- таблица Ozon по кабинетам ----
    oz_rows = []
    for name, c in sorted(oz_cab.items(), key=lambda kv: -kv[1]["cur"]["accruals_for_sale"]):
        rev = c["cur"]["accruals_for_sale"]
        if rev <= 0:
            continue
        growth = ((rev - c["prev"]["accruals_for_sale"]) / c["prev"]["accruals_for_sale"]
                  if c["prev"]["accruals_for_sale"] else None)
        gtxt = (f'<td class="pos">{"+" if growth >= 0 else ""}{pct(growth, 0)}</td>'
                if growth is not None else "<td>—</td>")
        oz_rows.append(
            f"<tr><td>{name}</td><td>{fr(rev)}</td><td>{pct(rev / oz_rev)}</td>"
            f'<td class="neu">{pct(-c["cur"]["sale_commission"] / rev)}</td>'
            f'<td>{pct(-c["cur"]["processing_and_delivery"] / rev)}</td>'
            f'<td>{pct(-c["cur"]["services_amount"] / rev)}</td>'
            f'<td>{fr(c["net"])}</td><td class="pos">{pct(c["net"] / rev)}</td>{gtxt}</tr>')
    oz_growth_txt = f"{'+' if oz_growth >= 0 else ''}{pct(oz_growth, 0)}"
    oz_table = "\n".join(oz_rows)
    oz_foot = (f"<tr><td>ИТОГО Ozon</td><td>{fr(oz_rev)}</td><td>100%</td>"
               f"<td>{pct(oz_comm_share)}</td><td>{pct(oz_log_share)}</td>"
               f"<td>{pct(oz_serv_share)}</td><td>{fr(oz_netto)}</td>"
               f"<td>{pct(oz_margin)}</td><td>{oz_growth_txt}</td></tr>")

    # ---- таблица WB по юрлицам ----
    wb_rows = []
    for slug in WB_ORDER:
        c = wb_cab.get(slug)
        if not c or c["ann"]["retail"] <= 0:
            continue
        a = c["ann"]
        adv = c["ded"].get("Внутренняя реклама", 0.0)
        wb_rows.append(
            f'<tr><td><span class="tag" style="background:{WB_COLORS[slug]}"></span>'
            f"{WB_LABELS[slug]}</td><td>{fr(a['retail'])}</td>"
            f"<td>{pct(a['retail'] / wb_retail)}</td><td>{fr(a['delivery'])}</td>"
            f'<td class="neu">{pct(a["delivery"] / a["retail"])}</td>'
            f"<td>{fr(a['deduction'])}</td><td>{fr(adv)}</td><td>{fr(c['net'])}</td>"
            f'<td class="pos">{pct(c["net"] / a["retail"])}</td></tr>')
    wb_table = "\n".join(wb_rows)
    wb_foot = (f"<tr><td>ИТОГО WB</td><td>{fr(wb_retail)}</td><td>100%</td>"
               f"<td>{fr(M['wb_delivery'])}</td><td>{pct(wb_log_share)}</td>"
               f"<td>{fr(M['wb_ded_total'])}</td><td>{fr(M['wb_adv'])}</td>"
               f"<td>{fr(wb_netto)}</td><td>{pct(wb_margin)}</td></tr>")

    # ---- удержания WB по кабинетам (таблица) ----
    ded_order = ["Внутренняя реклама", "Транзитные поставки", "Подписка «Джем»",
                 "Отзывы (баллы/выкуп)", "Утилизация", "Прочие удержания"]
    ded_rows = []
    for b in ded_order:
        if not any(wb_cab[s]["ded"].get(b) for s in wb_cab):
            continue
        cells = "".join(f"<td>{fr(wb_cab[s]['ded'].get(b, 0.0))}</td>"
                        for s in WB_ORDER if s in wb_cab)
        ded_rows.append(f"<tr><td>{b}</td>{cells}</tr>")
    ded_head = "".join(f"<th>{WB_LABELS[s]}</th>" for s in WB_ORDER if s in wb_cab)
    ded_foot = "".join(f"<td>{fr(wb_cab[s]['ann']['deduction'])}</td>"
                       for s in WB_ORDER if s in wb_cab)
    ded_table = "\n".join(ded_rows)

    # ---- топ SKU ----
    sku_rows = []
    for s in M["top_sku"]:
        per = s["delivery"] / s["qty"] if s["qty"] else 0
        sku_rows.append(
            f"<tr><td>{s['sa']}</td><td class=\"muted\">{s['subj']}</td>"
            f"<td>{fr(s['for_pay'])}</td><td class=\"neu\">{fr(s['delivery'])}</td>"
            f"<td>{fr(s['for_pay'] - s['delivery'])}</td><td>{fr(per)}</td>"
            f"<td>{fr(s['qty'])}</td></tr>")
    sku_table = "\n".join(sku_rows)

    # ---- KPI: средний чек по кабинетам WB ----
    kpi_cabs = []
    for slug in WB_ORDER:
        c = wb_cab.get(slug)
        if not c or not c["ann"]["qty"]:
            continue
        avg = c["ann"]["retail"] / c["ann"]["qty"]
        kpi_cabs.append(
            f'<div class="kpi"><div class="lab">{WB_LABELS[slug]}</div>'
            f'<div class="val">≈ {fr(avg)} ₽</div>'
            f'<div class="note">{tys(c["ann"]["qty"])} тыс. шт за период</div></div>')
    kpi_cabs_html = "\n".join(kpi_cabs)

    # ---- категории Ozon ----
    cat = defaultdict(float)
    for name, c in oz_cab.items():
        cat[OZ_CATEGORY.get(name, "Прочее")] += c["cur"]["accruals_for_sale"]
    cat_pairs = sorted(cat.items(), key=lambda kv: -kv[1])

    # ---- данные для графиков ----
    D = {
        "labels": labels,
        "split": [round(M["oz_rev"]), round(wb_retail)],
        "netSplit": {"labels": [f"Ozon {mln(oz_netto)} млн", f"WB {mln(wb_netto)} млн"],
                     "data": [round(oz_netto), round(wb_netto)]},
        "vs": [[round(wb_margin * 100, 1), round(oz_margin * 100, 1)],
               [round((1 - wb_margin) * 100, 1), round((1 - oz_margin) * 100, 1)]],
        "ozCab": {"labels": [n.replace("биг ОЗОН ", "биг ").replace("ОЗОН ", "")
                             for n, c in sorted(oz_cab.items(),
                                                key=lambda kv: -kv[1]["cur"]["accruals_for_sale"])
                             if c["cur"]["accruals_for_sale"] > 0],
                  "data": [round(c["cur"]["accruals_for_sale"])
                           for n, c in sorted(oz_cab.items(),
                                              key=lambda kv: -kv[1]["cur"]["accruals_for_sale"])
                           if c["cur"]["accruals_for_sale"] > 0]},
        "ozCat": {"labels": [k for k, v in cat_pairs], "data": [round(v) for k, v in cat_pairs]},
        "ozMonthly": {"cur": [round(v) for v in M["oz_m_cur"]],
                      "prev": [round(v) for v in M["oz_m_prev"]]},
        "ozCost": {"labels": ["Нетто продавцу", "Комиссия", "Логистика",
                              "Услуги/реклама", "Возвраты и прочее"],
                   "data": [round(oz_netto), round(M["oz_comm"]), round(M["oz_log"]),
                            round(M["oz_serv"]), max(0, round(M["oz_other"]))]},
        "wbShare": {"labels": [WB_LABELS[s] for s in WB_ORDER if s in wb_cab],
                    "data": [round(wb_cab[s]["ann"]["retail"]) for s in WB_ORDER if s in wb_cab],
                    "colors": [WB_COLORS[s] for s in WB_ORDER if s in wb_cab]},
        "wbMargin": {"cats": [WB_LABELS[s] for s in WB_ORDER if s in wb_cab],
                     "margin": [round(wb_cab[s]["net"] / wb_cab[s]["ann"]["retail"] * 100, 1)
                                if wb_cab[s]["ann"]["retail"] else 0
                                for s in WB_ORDER if s in wb_cab],
                     "logist": [round(wb_cab[s]["ann"]["delivery"] / wb_cab[s]["ann"]["retail"] * 100, 1)
                                if wb_cab[s]["ann"]["retail"] else 0
                                for s in WB_ORDER if s in wb_cab]},
        "wbMonthly": [{"name": WB_LABELS[s], "color": WB_COLORS[s],
                       "data": [round(v) for v in M["wb_m"][s]]}
                      for s in WB_ORDER if s in wb_cab],
        "trend": {"log": M["trend_log"], "margin": M["trend_margin"]},
        "hold": {"labels": [b for b in ded_order if M["ded_group"].get(b)],
                 "data": [round(M["ded_group"][b]) for b in ded_order if M["ded_group"].get(b)]},
    }

    warn_html = ""
    if warnings:
        items = "".join(f"<li>{w}</li>" for w in sorted(set(warnings)))
        warn_html = (f'<div class="flag">⚠️ Часть данных не обновилась из API:'
                     f'<ul style="margin:6px 0 0 18px;padding:0">{items}</ul></div>')

    style = STYLE
    body = f"""
<div class="wrap">
<header>
  <h1>Финансовый дашборд группы PastiLab</h1>
  <div class="sub">Две площадки: <b style="color:var(--wb)">Wildberries</b> (4 юрлица) + <b style="color:var(--ozon)">Ozon</b> (5 кабинетов) · данные из API</div>
  <div class="period">Период: {period} (скользящие 12 мес + текущий) &nbsp;·&nbsp; обновлено {upd} &nbsp;·&nbsp; * — месяц неполный</div>
</header>
{warn_html}

<div class="part">Часть I · <b>Обзор двух площадок</b></div>
<h2><span class="n">01</span> Группа на Wildberries и Ozon</h2>
<div class="kpis">
  <div class="kpi"><div class="lab">Оборот группы (обе площадки)</div><div class="val">{mln(total_rev)} млн ₽</div><div class="note">валовые продажи за период</div></div>
  <div class="kpi"><div class="lab">Нетто к перечислению</div><div class="val">{mln(total_net)} млн ₽</div><div class="note">после удержаний площадок</div></div>
  <div class="kpi"><div class="lab">Ozon</div><div class="val" style="color:var(--ozon)">{mln(oz_rev)} млн</div><div class="note">{pct(oz_rev / total_rev, 0)} оборота · {oz_growth_txt} г/г</div></div>
  <div class="kpi"><div class="lab">Wildberries</div><div class="val" style="color:var(--wb)">{mln(wb_retail)} млн</div><div class="note">{pct(wb_retail / total_rev, 0)} оборота · 4 юрлица</div></div>
  <div class="kpi"><div class="lab">Продано единиц (WB)</div><div class="val">{tys(M["wb_qty"])} тыс.</div><div class="note">+ объём Ozon</div></div>
</div>
<div class="grid2">
  <div class="card"><h3>Оборот по площадкам</h3><p class="cap">валовые продажи за период</p><div id="cSplit"></div></div>
  <div class="card"><h3>Нетто к перечислению по площадкам</h3><p class="cap">после удержаний площадок</p><div id="cNetSplit"></div></div>
</div>
<div class="flag b">ℹ️ «Оборот» = валовые продажи (WB retail_amount / Ozon «начислено за продажи»). «Нетто» = сумма к перечислению продавцу после удержаний площадки, но <b>до</b> себестоимости товара, налогов и расходов вне маркетплейса — это ещё не чистая прибыль.</div>

<h2><span class="n">02</span> Wildberries vs Ozon: сравнение площадок</h2>
<div class="card">
  <h3>Ключевые метрики за период</h3>
  <p class="cap">% указаны от валовых продаж соответствующей площадки.</p>
  <div style="overflow-x:auto">
  <table>
    <thead><tr><th>Показатель</th><th style="color:var(--wb)">Wildberries</th><th style="color:var(--ozon)">Ozon</th><th>Комментарий</th></tr></thead>
    <tbody>
      <tr><td>Оборот (валовые продажи)</td><td>{mln(wb_retail)} млн ₽</td><td>{mln(oz_rev)} млн ₽</td><td class="muted small">Ozon в {oz_rev / wb_retail:.1f}× больше</td></tr>
      <tr><td>Комиссия площадки</td><td class="muted">зашита в forPay*</td><td class="neu">{pct(oz_comm_share)}</td><td class="muted small">*WB удерживает комиссию до перечисления</td></tr>
      <tr><td>Логистика и обработка</td><td class="neu">{pct(wb_log_share)}</td><td class="pos">{pct(oz_log_share)}</td><td class="muted small">на WB логистика заметно дороже</td></tr>
      <tr><td>Реклама / услуги / хранение</td><td>{pct(wb_advst_share)}</td><td class="neu">{pct(oz_serv_share)}</td><td class="muted small">WB: внутр. реклама + хранение</td></tr>
      <tr><td>Возвраты и отмены</td><td class="pos">≈0%</td><td class="pos">{pct(oz_ref_share)}</td><td class="muted small">обе площадки — почти ноль</td></tr>
      <tr><td>Нетто-маржа (к перечислению)</td><td class="pos">{pct(wb_margin)}</td><td class="neu">{pct(oz_margin)}</td><td class="muted small">WB выгоднее на рубль продаж</td></tr>
      <tr><td>Нетто к перечислению</td><td>{mln(wb_netto)} млн ₽</td><td>{mln(oz_netto)} млн ₽</td><td class="muted small">Ozon даёт {oz_netto / wb_netto:.1f}× денег</td></tr>
      <tr><td>Рост год-к-году (оборот)</td><td class="muted">1-й год на WB</td><td class="pos">{oz_growth_txt}</td><td class="muted small">по данным finance API</td></tr>
    </tbody>
  </table>
  </div>
</div>
<div class="grid2">
  <div class="card"><h3>Кто сколько забирает</h3><p class="cap">Доля продавца (нетто) vs удержания площадки, % от продаж</p><div id="cVs"></div></div>
  <div class="card" style="display:flex;flex-direction:column;justify-content:center">
    <div style="font-size:13.5px;color:var(--mut);line-height:1.7">
      <b style="color:var(--ink)">Два разных двигателя.</b> <b style="color:var(--wb)">WB</b> отдаёт продавцу {pct(wb_margin, 0)} с рубля продаж при обороте {mln(wb_retail)} млн. <b style="color:var(--ozon)">Ozon</b> оставляет продавцу {pct(oz_margin, 0)} (комиссия {pct(oz_comm_share, 0)} + услуги {pct(oz_serv_share, 0)}), зато делает {mln(oz_rev)} млн оборота.<br><br>
      <b style="color:var(--ink)">Ozon — денежный локомотив группы</b> ({pct(oz_netto / total_net, 0)} всего нетто), <b style="color:var(--ink)">WB — маржинальный резерв</b>. Рычаги разные: на WB — снижать логистику, на Ozon — отбивать комиссию ценой и оборачиваемостью.
    </div>
  </div>
</div>

<div class="part">Часть II · <b style="color:var(--ozon)">Ozon</b> — {pct(oz_rev / total_rev, 0)} оборота группы</div>
<h2><span class="n">03</span> Ozon: структура по кабинетам</h2>
<div class="card">
  <h3>5 кабинетов Ozon за период</h3>
  <p class="cap">Все суммы в ₽. Маржа = нетто ÷ выручка. Рост — к тем же месяцам годом ранее.</p>
  <div style="overflow-x:auto">
  <table>
    <thead><tr><th>Кабинет</th><th>Выручка</th><th>Доля</th><th>Комиссия</th><th>Логистика</th><th>Услуги</th><th>Нетто</th><th>Маржа</th><th>Рост г/г</th></tr></thead>
    <tbody>{oz_table}</tbody>
    <tfoot>{oz_foot}</tfoot>
  </table>
  </div>
</div>
<div class="grid2">
  <div class="card"><h3>Выручка по кабинетам</h3><p class="cap">доля каждого кабинета в обороте Ozon</p><div id="cOzCab"></div></div>
  <div class="card"><h3>По категориям товара</h3><p class="cap">пастила / пасты и урбечи / батончики</p><div id="cOzCat"></div></div>
</div>

<h2><span class="n">04</span> Ozon: динамика и рост год-к-году</h2>
<div class="card">
  <h3>Выручка по месяцам: текущий год vs предыдущий</h3>
  <p class="cap">«Начислено за продажи» по 5 кабинетам, из finance API.</p>
  <div class="legend"><span><span class="tag" style="background:var(--ozon)"></span>Текущий год</span><span><span class="tag" style="background:var(--dim)"></span>Предыдущий год</span></div>
  <div id="cOzMonthly"></div>
</div>

<h2><span class="n">05</span> Ozon: структура затрат</h2>
<div class="grid2">
  <div class="card"><h3>Куда уходит рубль выручки Ozon</h3><p class="cap">{pct(1 - oz_margin, 0)} забирает площадка, {pct(oz_margin, 0)} остаётся продавцу</p><div id="cOzCost"></div></div>
  <div class="card" style="display:flex;flex-direction:column;justify-content:center">
    <div style="font-size:13.5px;color:var(--mut);line-height:1.7">
      <b style="color:var(--ink)">Главная статья Ozon — комиссия {pct(oz_comm_share)}</b> ({mln(M["oz_comm"])} млн ₽). Логистика — {pct(oz_log_share)} ({mln(M["oz_log"])} млн ₽), заметно дешевле, чем на WB. Услуги (реклама + хранение + продвижение) — {pct(oz_serv_share)} ({mln(M["oz_serv"])} млн ₽).<br><br>
      Возвраты и отмены — {pct(oz_ref_share)} от оборота: почти ноль, как и на WB.
    </div>
  </div>
</div>

<div class="part">Часть III · <b style="color:var(--wb)">Wildberries</b> — {pct(wb_retail / total_rev, 0)} оборота, 4 юрлица</div>
<h2><span class="n">06</span> WB: P&amp;L по юрлицам</h2>
<div class="card">
  <h3>Четыре юрлица на WB за период</h3>
  <p class="cap">Все суммы в ₽. «Маржа» = нетто к выплате ÷ розница (внутри WB). Источник: финотчёт-детализация.</p>
  <div style="overflow-x:auto">
  <table>
    <thead><tr><th>Юрлицо</th><th>Розница</th><th>Доля</th><th>Логистика</th><th>Лог.%</th><th>Удержания</th><th>Реклама</th><th>Нетто</th><th>Маржа</th></tr></thead>
    <tbody>{wb_table}</tbody>
    <tfoot>{wb_foot}</tfoot>
  </table>
  </div>
</div>
<div class="grid2">
  <div class="card"><h3>Доля розницы по юрлицам</h3><p class="cap">структура оборота WB</p><div id="cShare"></div></div>
  <div class="card"><h3>Маржа WB и доля логистики</h3><p class="cap">% от розницы соответствующего юрлица</p><div id="cMargin"></div></div>
</div>

<h2><span class="n">07</span> WB: помесячная динамика</h2>
<div class="card">
  <h3>Розница по месяцам с разбивкой по юрлицам</h3>
  <p class="cap">* — текущий месяц неполный.</p>
  <div class="legend"><span><span class="tag" style="background:var(--pasti)"></span>PastiLab</span><span><span class="tag" style="background:var(--tdbio)"></span>ТД БИО</span><span><span class="tag" style="background:var(--violet)"></span>ИП Кузнецова</span><span><span class="tag" style="background:var(--kaz)"></span>ИП Казакова</span></div>
  <div id="cMonthly"></div>
</div>

<h2><span class="n">08</span> WB: логистика и нетто-маржа по месяцам</h2>
<div class="card">
  <p class="cap">Доля логистики и нетто-маржа группы WB, % от розницы. Текущая логистика: {pct(wb_log_share)}.</p>
  <div class="legend"><span><span class="tag" style="background:var(--warn)"></span>Доля логистики, %</span><span><span class="tag" style="background:var(--good)"></span>Нетто-маржа, %</span></div>
  <div id="cTrend"></div>
</div>

<h2><span class="n">09</span> WB: структура удержаний</h2>
<div class="grid2">
  <div class="card"><h3>Удержания WB — вся группа</h3><p class="cap">{mln(M["wb_ded_total"])} млн ₽ за период</p><div id="cHold"></div></div>
  <div class="card">
    <h3>Расшифровка по юрлицам</h3><p class="cap">в ₽ за период</p>
    <div style="overflow-x:auto">
    <table>
      <thead><tr><th>Статья</th>{ded_head}</tr></thead>
      <tbody>{ded_table}</tbody>
      <tfoot><tr><td>Итого</td>{ded_foot}</tr></tfoot>
    </table>
    </div>
  </div>
</div>

<h2><span class="n">10</span> WB: топ-SKU и юнит-экономика</h2>
<div class="card">
  <h3>PastiLab — топ-10 SKU по перечислению</h3>
  <p class="cap">«Лог./ед.» — логистика на единицу проданного.</p>
  <div style="overflow-x:auto">
  <table>
    <thead><tr><th>Артикул</th><th>Предмет</th><th>К перечисл. ₽</th><th>Логистика ₽</th><th>После логистики ₽</th><th>Лог./ед. ₽</th><th>Шт</th></tr></thead>
    <tbody>{sku_table}</tbody>
  </table>
  </div>
  <div class="kpis" style="margin-top:16px">
    <div class="kpi"><div class="lab">Продано (WB, группа)</div><div class="val">{tys(M["wb_qty"])} тыс.</div><div class="note">шт за период · средний чек по рознице:</div></div>
    {kpi_cabs_html}
  </div>
</div>

<div class="foot">
  Источники: WB Statistics API (reportDetailByPeriod, 4 юрлица) · Ozon Seller API (finance/transaction/totals, 5 кабинетов).<br>
  Обновлено {upd}. Закрытые месяцы кэшируются; текущий и незакрытые месяцы перезапрашиваются при каждом обновлении.<br>
  Реклама WB = удержания «ВБ.Продвижение» из финотчёта; реклама с отдельного рекламного счёта сюда не попадает.<br>
  Разделы про географию заказов, склады и выкуп (11–12 старого отчёта) не автообновляются и в живую версию не включены.
</div>
</div>

<script>
const C={{wb:'#5b8def',ozon:'#a78bfa',pasti:'#5b8def',tdbio:'#f0a63c',kaz:'#3ec6a0',good:'#3ec6a0',warn:'#f0a63c',bad:'#ef6b6b',violet:'#a78bfa',dim:'#6b7684',grid:'#2a3038',ink:'#e8ecf1',mut:'#9aa4b2'}};
const D={json.dumps(D, ensure_ascii=False)};
{CHART_JS}
donut('cSplit',['Ozon','Wildberries'],D.split,[C.ozon,C.wb]);
donut('cNetSplit',D.netSplit.labels,D.netSplit.data,[C.ozon,C.wb]);
groupBar('cVs',['Wildberries','Ozon'],[
  {{name:'Продавцу',color:C.good,data:D.vs[0]}},
  {{name:'Площадке',color:C.bad,data:D.vs[1]}}
]);
donut('cOzCab',D.ozCab.labels,D.ozCab.data,[C.ozon,'#8b6fe0','#f0a63c','#ef6b6b','#3ec6a0']);
donut('cOzCat',D.ozCat.labels,D.ozCat.data,[C.ozon,'#f0a63c','#3ec6a0','#6b7684']);
groupMonthly('cOzMonthly',D.labels,D.ozMonthly.cur,D.ozMonthly.prev,C.ozon,C.dim);
donut('cOzCost',D.ozCost.labels,D.ozCost.data,[C.good,C.bad,C.warn,C.violet,C.dim]);
donut('cShare',D.wbShare.labels,D.wbShare.data,D.wbShare.colors);
groupBar('cMargin',D.wbMargin.cats,[
  {{name:'Маржа WB',color:C.good,data:D.wbMargin.margin}},
  {{name:'Логистика',color:C.warn,data:D.wbMargin.logist}}
]);
stackBar('cMonthly',D.labels,D.wbMonthly);
lineDual('cTrend',D.labels,[
  {{name:'Логистика',color:C.warn,data:D.trend.log}},
  {{name:'Маржа',color:C.good,data:D.trend.margin}}
]);
donut('cHold',D.hold.labels,D.hold.data,[C.pasti,C.tdbio,C.kaz,C.violet,'#6b7684','#4a5563']);
</script>"""

    title = "Финансовый дашборд PastiLab — WB + Ozon (live)"
    artifact = f"<title>{title}</title>\n<style>{style}</style>\n{body}"
    standalone = (f'<!DOCTYPE html>\n<html lang="ru">\n<head>\n<meta charset="UTF-8">\n'
                  f'<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
                  f'<title>{title}</title>\n<style>{style}</style>\n</head>\n<body>\n'
                  f'{body}\n</body>\n</html>')
    return standalone, artifact


STYLE = """
  :root{
    --bg:#0f1216; --panel:#171b21; --panel2:#1d222a; --line:#2a3038;
    --ink:#e8ecf1; --mut:#9aa4b2; --dim:#6b7684;
    --wb:#5b8def; --ozon:#a78bfa; --pasti:#5b8def; --tdbio:#f0a63c; --kaz:#3ec6a0;
    --good:#3ec6a0; --warn:#f0a63c; --bad:#ef6b6b; --accent:#5b8def; --violet:#a78bfa;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Inter,Arial,sans-serif;
    -webkit-font-smoothing:antialiased;line-height:1.5}
  .wrap{max-width:1120px;margin:0 auto;padding:32px 20px 80px}
  h1{font-size:26px;margin:0 0 4px;letter-spacing:-.3px}
  .sub{color:var(--mut);font-size:14px}
  .period{display:inline-block;margin-top:10px;font-size:12px;color:var(--dim);
    background:var(--panel2);border:1px solid var(--line);padding:4px 10px;border-radius:20px}
  .part{margin:44px 0 6px;font-size:12px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--dim)}
  .part b{color:var(--ink)}
  h2{font-size:17px;margin:26px 0 14px;padding-bottom:8px;border-bottom:1px solid var(--line);
    letter-spacing:-.2px;display:flex;align-items:center;gap:9px}
  h2 .n{font-size:12px;color:var(--dim);font-weight:600;background:var(--panel2);
    border:1px solid var(--line);border-radius:6px;padding:2px 8px}
  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));gap:12px}
  .kpi{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:15px 16px}
  .kpi .lab{font-size:12px;color:var(--mut);margin-bottom:7px}
  .kpi .val{font-size:23px;font-weight:700;letter-spacing:-.5px}
  .kpi .note{font-size:11.5px;color:var(--dim);margin-top:5px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px 18px 16px;margin-top:16px}
  .card h3{margin:0 0 2px;font-size:15px}
  .card .cap{color:var(--mut);font-size:12.5px;margin:0 0 12px}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  @media(max-width:820px){.grid2{grid-template-columns:1fr}}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{padding:9px 10px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
  th:first-child,td:first-child{text-align:left}
  thead th{color:var(--mut);font-weight:600;font-size:11.5px;text-transform:uppercase;letter-spacing:.4px}
  tbody tr:hover{background:var(--panel2)}
  tfoot td{font-weight:700;border-top:2px solid var(--line);border-bottom:none}
  .tag{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:7px;vertical-align:middle}
  .pos{color:var(--good)} .neg{color:var(--bad)} .neu{color:var(--warn)}
  .muted{color:var(--mut)} .small{font-size:12px}
  .flag{background:#2a2118;border:1px solid #4a3a22;color:#f0c98c;border-radius:10px;
    padding:11px 14px;font-size:12.5px;margin-top:14px}
  .flag.b{background:#141c2a;border-color:#25405e;color:#9cc4f0}
  .foot{margin-top:40px;color:var(--dim);font-size:11.5px;text-align:center;line-height:1.7}
  .legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--mut);margin:4px 0 10px}
  .legend span{display:flex;align-items:center}
  svg{display:block;width:100%;height:auto}
  .stxt{fill:var(--mut);font-size:11px}
  .sval{fill:var(--ink);font-size:11px}
  .gl{stroke:var(--line);stroke-width:1}
"""

CHART_JS = """
const NS='http://www.w3.org/2000/svg';
const mlnJ=v=>(v/1e6).toFixed(1).replace('.',',');
function svg(w,h){const s=document.createElementNS(NS,'svg');s.setAttribute('viewBox','0 0 '+w+' '+h);s.setAttribute('width',w);s.setAttribute('height',h);return s;}
function el(t,a,txt){const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);if(txt!=null)e.textContent=txt;return e;}
function mount(id,s){const n=document.getElementById(id);if(n)n.appendChild(s);}
function donut(id,labels,data,colors){
  const W=460,H=250,cx=118,cy=125,r=95,ir=57;
  const s=svg(W,H);const tot=data.reduce((a,b)=>a+b,0)||1;let ang=-Math.PI/2;
  data.forEach((v,i)=>{
    const a2=ang+v/tot*Math.PI*2;
    const x1=cx+r*Math.cos(ang),y1=cy+r*Math.sin(ang),x2=cx+r*Math.cos(a2),y2=cy+r*Math.sin(a2);
    const xi1=cx+ir*Math.cos(a2),yi1=cy+ir*Math.sin(a2),xi2=cx+ir*Math.cos(ang),yi2=cy+ir*Math.sin(ang);
    const large=(a2-ang)>Math.PI?1:0;
    s.appendChild(el('path',{d:`M${x1} ${y1}A${r} ${r} 0 ${large} 1 ${x2} ${y2}L${xi1} ${yi1}A${ir} ${ir} 0 ${large} 0 ${xi2} ${yi2}Z`,fill:colors[i%colors.length],stroke:'#171b21','stroke-width':2}));
    ang=a2;
  });
  let ly=42;labels.forEach((l,i)=>{
    s.appendChild(el('rect',{x:250,y:ly-9,width:11,height:11,rx:2,fill:colors[i%colors.length]}));
    s.appendChild(el('text',{x:268,y:ly,class:'sval','font-size':11.5},l));
    s.appendChild(el('text',{x:452,y:ly,class:'stxt','text-anchor':'end'},(data[i]/tot*100).toFixed(1)+'%'));
    ly+=25;
  });
  mount(id,s);
}
function groupBar(id,cats,series){
  const W=460,H=250,pl=34,pr=12,pt=14,pb=34;const iw=W-pl-pr,ih=H-pt-pb;
  const s=svg(W,H);const max=Math.max(...series.flatMap(x=>x.data))*1.15;
  [0,.25,.5,.75,1].forEach(t=>{const y=pt+ih*(1-t);
    s.appendChild(el('line',{x1:pl,y1:y,x2:W-pr,y2:y,class:'gl'}));
    s.appendChild(el('text',{x:pl-6,y:y+3,class:'stxt','text-anchor':'end'},Math.round(max*t)+'%'));});
  const gw=iw/cats.length,bw=gw*0.6/series.length;
  cats.forEach((c,i)=>{
    const gx=pl+gw*i+gw*0.2;
    series.forEach((se,j)=>{
      const v=se.data[i],bh=v/max*ih,x=gx+bw*j,y=pt+ih-bh;
      s.appendChild(el('rect',{x,y,width:bw-3,height:bh,rx:3,fill:se.color}));
      s.appendChild(el('text',{x:x+(bw-3)/2,y:y-4,class:'sval','text-anchor':'middle','font-size':10},v+'%'));
    });
    s.appendChild(el('text',{x:pl+gw*i+gw/2,y:H-12,class:'stxt','text-anchor':'middle'},c));
  });
  mount(id,s);
}
function stackBar(id,cats,series){
  const W=1040,H=340,pl=40,pt=14,pb=40;const prr=14,iw=W-pl-prr,ih=H-pt-pb;
  const s=svg(W,H);
  const totals=cats.map((_,i)=>series.reduce((a,se)=>a+se.data[i],0));
  const max=Math.max(...totals)*1.08||1;
  [0,.25,.5,.75,1].forEach(t=>{const y=pt+ih*(1-t);
    s.appendChild(el('line',{x1:pl,y1:y,x2:W-prr,y2:y,class:'gl'}));
    s.appendChild(el('text',{x:pl-6,y:y+3,class:'stxt','text-anchor':'end'},(max*t/1e6).toFixed(0)+'M'));});
  const gw=iw/cats.length,bw=Math.min(46,gw*0.6);
  cats.forEach((c,i)=>{
    const x=pl+gw*i+(gw-bw)/2;let yb=pt+ih;
    series.forEach(se=>{const v=se.data[i],bh=v/max*ih;yb-=bh;
      s.appendChild(el('rect',{x,y:yb,width:bw,height:bh,fill:se.color}));});
    s.appendChild(el('text',{x:x+bw/2,y:yb-5,class:'sval','text-anchor':'middle','font-size':10},mlnJ(totals[i]).replace(',0','')));
    s.appendChild(el('text',{x:pl+gw*i+gw/2,y:H-14,class:'stxt','text-anchor':'middle'},c));
  });
  mount(id,s);
}
function groupMonthly(id,cats,a,b,ca,cb){
  const W=1040,H=320,pl=40,pt=14,pb=40,prr=14;const iw=W-pl-prr,ih=H-pt-pb;
  const s=svg(W,H);const max=Math.max(...a,...b)*1.1||1;
  [0,.25,.5,.75,1].forEach(t=>{const y=pt+ih*(1-t);
    s.appendChild(el('line',{x1:pl,y1:y,x2:W-prr,y2:y,class:'gl'}));
    s.appendChild(el('text',{x:pl-6,y:y+3,class:'stxt','text-anchor':'end'},(max*t/1e6).toFixed(0)+'M'));});
  const gw=iw/cats.length,bw=gw*0.62/2;
  cats.forEach((c,i)=>{
    const gx=pl+gw*i+gw*0.19;
    [[a[i],ca],[b[i],cb]].forEach((d,j)=>{
      const bh=d[0]/max*ih;s.appendChild(el('rect',{x:gx+bw*j,y:pt+ih-bh,width:bw-2,height:bh,rx:2,fill:d[1]}));
    });
    s.appendChild(el('text',{x:pl+gw*i+gw/2,y:H-14,class:'stxt','text-anchor':'middle','font-size':10},c));
  });
  mount(id,s);
}
function lineDual(id,cats,series){
  const W=1040,H=300,pl=36,prr=14,pt=16,pb=40;const iw=W-pl-prr,ih=H-pt-pb;const max=90;
  const s=svg(W,H);
  [0,.25,.5,.75,1].forEach(t=>{const y=pt+ih*(1-t);
    s.appendChild(el('line',{x1:pl,y1:y,x2:W-prr,y2:y,class:'gl'}));
    s.appendChild(el('text',{x:pl-6,y:y+3,class:'stxt','text-anchor':'end'},Math.round(max*t)+'%'));});
  const xx=i=>pl+iw*(i/(cats.length-1)),yy=v=>pt+ih*(1-Math.max(0,Math.min(max,v))/max);
  cats.forEach((c,i)=>s.appendChild(el('text',{x:xx(i),y:H-14,class:'stxt','text-anchor':'middle','font-size':10},c)));
  series.forEach(se=>{let d='';se.data.forEach((v,i)=>d+=(i?'L':'M')+xx(i)+' '+yy(v));
    s.appendChild(el('path',{d,fill:'none',stroke:se.color,'stroke-width':2.5}));
    se.data.forEach((v,i)=>s.appendChild(el('circle',{cx:xx(i),cy:yy(v),r:3,fill:se.color})));});
  mount(id,s);
}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true",
                    help="только рендер из кэша, без запросов к API")
    ap.add_argument("--to", help="дата 'сегодня' YYYY-MM-DD (для отладки)")
    args = ap.parse_args()

    os.makedirs(CACHE, exist_ok=True)
    os.makedirs(OUT, exist_ok=True)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])

    today = dt.date.fromisoformat(args.to) if args.to else dt.date.today()
    months = window_months(today)
    warnings = []

    log.info("Окно: %s .. %s (%s мес.)", months[0], months[-1], len(months))
    oz = load_ozon(months, today, args.offline, warnings)
    wb = load_wb(months, today, args.offline, warnings)

    M = build_model(months, today, wb, oz)
    standalone, artifact = render(M, months, today, warnings)

    p1 = os.path.join(OUT, "dashboard.html")
    p2 = os.path.join(OUT, "artifact.html")
    with open(p1, "w", encoding="utf-8") as f:
        f.write(standalone)
    with open(p2, "w", encoding="utf-8") as f:
        f.write(artifact)
    log.info("ГОТОВО: %s | %s", p1, p2)
    if warnings:
        log.warning("Предупреждения: %s", "; ".join(sorted(set(warnings))))


if __name__ == "__main__":
    main()

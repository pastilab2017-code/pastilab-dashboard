"""
Коннектор к Wildberries Statistics API (statistics-api.wildberries.ru).

Один экземпляр = один кабинет (токен). Токен НЕ логируется (mask()).
Эндпоинты «Статистики» возвращают историю на 2+ года и лимитируют выдачу
~80 000 строк за запрос — поэтому пагинация идёт по lastChangeDate/rrd_id.
Rate limit статистики — порядка 1 запроса/мин, поэтому есть паузы и ретраи на 429.
"""

import json
import logging
import time
import requests

BASE = "https://statistics-api.wildberries.ru"
log = logging.getLogger("wb")


def mask(token: str) -> str:
    if not token or len(token) < 24:
        return "****"
    return f"{token[:12]}...{token[-8:]}"


class WBStatClient:
    def __init__(self, name: str, token: str, timeout: int = 180,
                 max_retries: int = 6, pause: float = 22.0):
        self.name = name
        self.token = token
        self.timeout = timeout
        self.max_retries = max_retries
        self.pause = pause              # пауза между страницами (rate limit ~1/мин)
        self.s = requests.Session()
        self.s.headers.update({"Authorization": token})

    def _get(self, path: str, params: dict) -> list | dict:
        attempt = 0
        while True:
            attempt += 1
            try:
                r = self.s.get(BASE + path, params=params, timeout=self.timeout)
            except requests.RequestException as e:
                if attempt >= self.max_retries:
                    raise
                wait = min(2 ** attempt, 60)
                log.warning("[%s] %s сеть (%s), повтор через %ss",
                            self.name, path, e.__class__.__name__, wait)
                time.sleep(wait)
                continue
            if r.status_code == 200:
                return r.json()
            if r.status_code == 204:      # нет данных / конец выдачи — не ошибка
                return []
            if r.status_code == 429:
                raw = int(r.headers.get("X-Ratelimit-Retry", 0) or 0)
                wait = min(raw or 60, 90)   # не ждём абсурдные Retry (у stocks бывает 80000+)
                log.warning("[%s] %s 429 rate limit (Retry=%s), ждём %ss (попытка %s)",
                            self.name, path, raw, wait, attempt)
                if attempt >= self.max_retries:
                    r.raise_for_status()
                time.sleep(wait)
                continue
            if r.status_code >= 500 and attempt < self.max_retries:
                wait = min(2 ** attempt, 60)
                log.warning("[%s] %s HTTP %s, повтор через %ss",
                            self.name, path, r.status_code, wait)
                time.sleep(wait)
                continue
            raise RuntimeError(f"[{self.name}] {path} HTTP {r.status_code}: {r.text[:300]}")

    @staticmethod
    def _rowkey(d: dict) -> str:
        return json.dumps(d, sort_keys=True, ensure_ascii=False)

    # ---------- Заказы / Продажи (пагинация по lastChangeDate) ----------
    def _paginate_by_change(self, path: str, date_from_iso: str) -> list[dict]:
        seen, out, cursor, page = set(), [], date_from_iso, 0
        while True:
            page += 1
            batch = self._get(path, {"dateFrom": cursor, "flag": 0})
            if not isinstance(batch, list) or not batch:
                break
            new = 0
            max_change = cursor
            for row in batch:
                k = self._rowkey(row)
                if k not in seen:
                    seen.add(k); out.append(row); new += 1
                lc = row.get("lastChangeDate")
                if lc and lc > max_change:
                    max_change = lc
            log.info("[%s] %s стр.%s: +%s (всего %s), cursor=%s",
                     self.name, path.rsplit('/', 1)[-1], page, new, len(out), max_change)
            # если сдвига по времени нет или все дубли — выходим
            if max_change == cursor or new == 0:
                break
            cursor = max_change
            time.sleep(self.pause)
        return out

    def _by_month(self, path: str, date_from: str, date_to: str) -> list[dict]:
        """Помесячные окна + пагинация по lastChangeDate внутри окна.

        Endpoint режет выдачу ~80k строк, поэтому годовой запрос теряет старую
        историю. Месячное окно почти всегда < 80k -> история не обрезается.
        Дедуп по всему содержимому строки (границы окон могут пересекаться).
        """
        import datetime as _dt
        a = _dt.date.fromisoformat(date_from[:10])
        end = _dt.date.fromisoformat(date_to[:10])
        seen, out = set(), []
        while a <= end:
            m_end = (a.replace(day=28) + _dt.timedelta(days=4)).replace(day=1) - _dt.timedelta(days=1)
            b = min(m_end, end)
            cursor = a.isoformat() + "T00:00:00"
            limit_iso = b.isoformat() + "T23:59:59"
            page = 0
            while True:
                page += 1
                batch = self._get(path, {"dateFrom": cursor, "flag": 0})
                if not isinstance(batch, list) or not batch:
                    break
                new, max_change = 0, cursor
                for row in batch:
                    d = row.get("date", "")
                    if d and d[:10] > b.isoformat():
                        continue          # вышли за пределы окна — не берём
                    k = self._rowkey(row)
                    if k not in seen:
                        seen.add(k); out.append(row); new += 1
                    lc = row.get("lastChangeDate")
                    if lc and lc > max_change:
                        max_change = lc
                mx = min(max_change, limit_iso)
                log.info("[%s] %s %s: стр.%s +%s (всего %s)",
                         self.name, path.rsplit('/', 1)[-1], a.strftime("%Y-%m"),
                         page, new, len(out))
                if mx <= cursor or new == 0 or max_change > limit_iso:
                    break
                cursor = mx
                time.sleep(self.pause)
            a = b + _dt.timedelta(days=1)
            time.sleep(self.pause)
        return out

    def orders(self, date_from_iso: str, date_to_iso: str = None) -> list[dict]:
        if date_to_iso:
            return self._by_month("/api/v1/supplier/orders", date_from_iso, date_to_iso)
        return self._paginate_by_change("/api/v1/supplier/orders", date_from_iso)

    def sales(self, date_from_iso: str, date_to_iso: str = None) -> list[dict]:
        if date_to_iso:
            return self._by_month("/api/v1/supplier/sales", date_from_iso, date_to_iso)
        return self._paginate_by_change("/api/v1/supplier/sales", date_from_iso)

    # ---------- Остатки (снимок) ----------
    def stocks(self) -> list[dict]:
        data = self._get("/api/v1/supplier/stocks",
                         {"dateFrom": "2020-01-01T00:00:00"})
        return data if isinstance(data, list) else []

    # ---------- Финансовый отчёт-детализация (пагинация по rrd_id) ----------
    def _report_window(self, date_from: str, date_to: str) -> list[dict]:
        """Один интервал дат; пагинация по rrd_id, меньший limit -> лёгкие ответы."""
        out, rrdid, page = [], 0, 0
        while True:
            page += 1
            batch = self._get("/api/v5/supplier/reportDetailByPeriod",
                              {"dateFrom": date_from, "dateTo": date_to,
                               "rrdid": rrdid, "limit": 30000})
            if not isinstance(batch, list) or not batch:
                break
            out.extend(batch)
            rrdid = batch[-1].get("rrd_id") or 0
            log.info("[%s] report %s..%s стр.%s: +%s (всего %s), rrd_id=%s",
                     self.name, date_from, date_to, page, len(batch), len(out), rrdid)
            if not rrdid:
                break
            time.sleep(self.pause)
        return out

    def report_detail(self, date_from: str, date_to: str) -> list[dict]:
        """Тянем помесячными окнами — годовой ответ одним куском рвёт соединение."""
        import datetime as _dt
        a = _dt.date.fromisoformat(date_from)
        end = _dt.date.fromisoformat(date_to)
        out = []
        while a <= end:
            m_end = (a.replace(day=28) + _dt.timedelta(days=4)).replace(day=1) - _dt.timedelta(days=1)
            b = min(m_end, end)
            try:
                out.extend(self._report_window(a.isoformat(), b.isoformat()))
            except Exception as e:
                log.warning("[%s] report окно %s..%s пропущено: %s", self.name, a, b, e)
            a = b + _dt.timedelta(days=1)
            time.sleep(self.pause)
        return out

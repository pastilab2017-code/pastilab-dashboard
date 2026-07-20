"""
Коннектор к Ozon Seller API (api-seller.ozon.ru).

Модульный клиент: один экземпляр = один кабинет (Client-Id + Api-Key).
Спроектирован так, чтобы рядом можно было добавить коннекторы WB Statistics /
Advertising и Ozon Performance API, не трогая этот код.

Особенности, выявленные при разведке API (2026-07):
- /v1/analytics/data отдаёт данные только за последние ~3 месяца — историческую
  воронку год-к-году получить нельзя (ограничение на стороне Ozon).
- /v3/finance/transaction/totals и /list хранят историю на 2+ года — на них
  строится сравнение YoY по деньгам.
- Токены НЕ логируются (см. mask()).
"""

import logging
import time
import requests

BASE_URL = "https://api-seller.ozon.ru"
log = logging.getLogger("ozon")


def mask(token: str) -> str:
    """Маскирует Api-Key для логов: 6665ea01-****-****-****-4b997ad1af95."""
    if not token or len(token) < 20:
        return "****"
    return f"{token[:8]}-****-****-****-{token[-12:]}"


class OzonSellerClient:
    def __init__(self, name: str, client_id: str, api_key: str,
                 timeout: int = 90, max_retries: int = 5):
        self.name = name
        self.client_id = str(client_id)
        self.api_key = str(api_key)
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update({
            "Client-Id": self.client_id,
            "Api-Key": self.api_key,
            "Content-Type": "application/json",
        })

    def _post(self, path: str, body: dict) -> dict:
        url = f"{BASE_URL}{path}"
        attempt = 0
        while True:
            attempt += 1
            try:
                r = self.session.post(url, json=body, timeout=self.timeout)
            except requests.RequestException as e:
                if attempt >= self.max_retries:
                    raise
                wait = min(2 ** attempt, 30)
                log.warning("[%s] %s сетевая ошибка (%s), повтор через %ss",
                            self.name, path, e.__class__.__name__, wait)
                time.sleep(wait)
                continue

            if r.status_code == 200:
                return r.json()

            # Rate limit
            if r.status_code == 429:
                retry_after = int(r.headers.get("Retry-After", "0") or 0)
                wait = retry_after or min(2 ** attempt, 60)
                log.warning("[%s] %s 429 rate limit, повтор через %ss (попытка %s)",
                            self.name, path, wait, attempt)
                if attempt >= self.max_retries:
                    r.raise_for_status()
                time.sleep(wait)
                continue

            # Временные ошибки сервера
            if r.status_code >= 500 and attempt < self.max_retries:
                wait = min(2 ** attempt, 30)
                log.warning("[%s] %s HTTP %s, повтор через %ss",
                            self.name, path, r.status_code, wait)
                time.sleep(wait)
                continue

            # Прочие ошибки — прокидываем с телом ответа (без токенов)
            raise RuntimeError(
                f"[{self.name}] {path} HTTP {r.status_code}: {r.text[:500]}")

    # ---------- Каталог ----------
    def product_list(self) -> list[dict]:
        """Все товары кабинета (offer_id, product_id, archived)."""
        items, last_id = [], ""
        while True:
            body = {"filter": {}, "last_id": last_id, "limit": 1000}
            res = self._post("/v3/product/list", body)["result"]
            batch = res.get("items", [])
            items.extend(batch)
            last_id = res.get("last_id", "")
            if not batch or not last_id or len(items) >= res.get("total", 0):
                break
        return items

    def product_info_list(self, offer_ids: list[str]) -> list[dict]:
        """Детали товаров (name, barcode, sku, price, видимость) чанками по 1000."""
        out = []
        for i in range(0, len(offer_ids), 1000):
            chunk = offer_ids[i:i + 1000]
            body = {"offer_id": chunk, "product_id": [], "sku": []}
            res = self._post("/v3/product/info/list", body)
            out.extend(res.get("items", []))
        return out

    # ---------- Аналитика (воронка) ----------
    def analytics_data(self, date_from: str, date_to: str,
                       metrics: list[str], dimension: list[str],
                       pause: float = 1.0) -> list[dict]:
        """
        /v1/analytics/data с пагинацией по offset.
        ВНИМАНИЕ: Ozon отдаёт только последние ~3 месяца истории.
        """
        rows, offset, limit = [], 0, 1000
        while True:
            body = {
                "date_from": date_from, "date_to": date_to,
                "metrics": metrics, "dimension": dimension,
                "filters": [], "sort": [], "limit": limit, "offset": offset,
            }
            data = self._post("/v1/analytics/data", body)["result"]["data"]
            rows.extend(data)
            if len(data) < limit:
                break
            offset += limit
            time.sleep(pause)
        return rows

    # ---------- Финансы ----------
    def finance_totals(self, date_from_iso: str, date_to_iso: str) -> dict:
        """Итоги по категориям начислений/удержаний за период (одним запросом)."""
        body = {
            "date": {"from": date_from_iso, "to": date_to_iso},
            "posting_number": "", "transaction_type": "all",
        }
        return self._post("/v3/finance/transaction/totals", body)["result"]

    def finance_transactions(self, date_from_iso: str, date_to_iso: str,
                             pause: float = 0.3) -> list[dict]:
        """Все операции за период с пагинацией по страницам (page_size=1000)."""
        ops, page = [], 1
        while True:
            body = {
                "filter": {
                    "date": {"from": date_from_iso, "to": date_to_iso},
                    "operation_type": [], "posting_number": "",
                    "transaction_type": "all",
                },
                "page": page, "page_size": 1000,
            }
            res = self._post("/v3/finance/transaction/list", body)["result"]
            ops.extend(res.get("operations", []))
            if page >= res.get("page_count", 1):
                break
            page += 1
            time.sleep(pause)
        return ops

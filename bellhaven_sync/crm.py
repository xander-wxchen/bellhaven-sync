"""Thin client for the CRM sandbox API.

Reads retry on network errors. Writes do NOT auto-retry: a retried POST could
create a second account. Callers decide (apply.py checks before re-creating).
"""
from __future__ import annotations

import time

import requests

from . import config


class CRMError(RuntimeError):
    def __init__(self, msg: str, status: int | None = None, body: str = ""):
        super().__init__(msg)
        self.status, self.body = status, body


def _unwrap(payload):
    if isinstance(payload, dict) and "data" in payload and isinstance(payload["data"], dict):
        return payload["data"]
    return payload


class CRMClient:
    def __init__(self, token: str | None = None, base: str | None = None, session=None):
        token = token or config.CRM_TOKEN
        if not token:
            raise CRMError("CRM_TOKEN is not set (see .env.example)")
        self.base = (base or config.CRM_API).rstrip("/")
        self.s = session or requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}",
                               "User-Agent": config.USER_AGENT})

    # -------------------------------------------------------------- low level
    def _request(self, method: str, path: str, *, retry: bool, **kw):
        url = f"{self.base}{path}"
        attempts = config.HTTP_RETRIES if retry else 1
        last = None
        for i in range(1, attempts + 1):
            try:
                r = self.s.request(method, url, timeout=config.HTTP_TIMEOUT, **kw)
            except requests.RequestException as e:
                last = CRMError(f"{method} {path} failed: {e}")
                time.sleep(i)
                continue
            if r.status_code >= 500 and retry and i < attempts:
                time.sleep(i)
                continue
            if r.status_code >= 400:
                raise CRMError(f"{method} {path} -> HTTP {r.status_code}", r.status_code, r.text[:2000])
            return r.json() if r.content else {}
        raise last or CRMError(f"{method} {path} failed")

    def _paginate(self, path: str, params: dict) -> list[dict]:
        rows, page = [], 1
        while True:
            body = self._request("GET", path, retry=True,
                                 params={**params, "page": page, "page_size": config.CRM_PAGE_SIZE})
            batch = body.get("data", [])
            rows += batch
            total = body.get("total")
            if not batch or (total is not None and len(rows) >= total):
                return rows
            page += 1

    # -------------------------------------------------------------- accounts
    def list_accounts(self, **filters) -> list[dict]:
        return self._paginate("/accounts", filters)

    def get_account(self, account_id: str) -> dict:
        return _unwrap(self._request("GET", f"/accounts/{account_id}", retry=True))

    def update_account(self, account_id: str, fields: dict) -> dict:
        return _unwrap(self._request("PATCH", f"/accounts/{account_id}", retry=False, json=fields))

    def create_account(self, fields: dict) -> dict:
        return _unwrap(self._request("POST", "/accounts", retry=False, json=fields))

    # -------------------------------------------------------------- contacts
    def list_contacts(self, **filters) -> list[dict]:
        return self._paginate("/contacts", filters)

    def get_contact(self, contact_id: str) -> dict:
        return _unwrap(self._request("GET", f"/contacts/{contact_id}", retry=True))

    def update_contact(self, contact_id: str, fields: dict) -> dict:
        return _unwrap(self._request("PATCH", f"/contacts/{contact_id}", retry=False, json=fields))

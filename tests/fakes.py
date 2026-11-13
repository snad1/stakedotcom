"""Fake Stake casino for tests (E0-S2).

Speaks the shapes the clients parse (stake.py and tg/engine.py `_limbo_parse`
/ `_dice_parse`, `api_get_balances`, `_cf_reauth` / `_api_test_connection`):

    POST {base}/_api/casino/limbo/bet {multiplierTarget, identifier, amount, currency}
        200 {"limboBet": {amount, payout, payoutMultiplier, result, state{result, multiplierTarget}}}
    POST {base}/_api/casino/dice/roll {target, condition above|below, identifier, amount, currency}
        200 {"diceRoll": {amount, payout, payoutMultiplier, result, state{result, target, condition}}}
    POST {base}/_api/graphql operationName AuthCheck  -> data.user{id}
                             operationName UserBalances -> data.user.balances[{available{amount, currency}}]
    base = https://stake.bet or https://stake.com

No balance in the bet response: both clients track it locally, so the fake's
ledger (`balance`) is what local tracking must equal.
Read-only GraphQL calls never consume the script.

Script outcomes: win, loss, insufficient_balance, http_429, timeout, cf_403.
cf_403 answers one bet POST with a Cloudflare challenge page; the next bet
POST (the TG retry after `_cf_reauth`, or the CLI's next loop) takes the next
outcome. An empty script answers insufficient_balance and sets `exhausted`.
Timeouts surface as curl_cffi Timeout (CLI, its real transport) /
httpx.ReadTimeout (TG, both caught by the same except clause).
"""

import json
from collections import deque

import httpx
from curl_cffi.requests.exceptions import Timeout as CffiTimeout

BASES = ("https://stake.bet", "https://stake.com")
LIMBO = "/_api/casino/limbo/bet"
DICE = "/_api/casino/dice/roll"
GQL = "/_api/graphql"
OUTCOMES = {"win", "loss", "insufficient_balance", "http_429", "timeout", "cf_403"}
CF_PAGE = "<!DOCTYPE html><html><head><title>Just a moment...</title></head></html>"


class UnexpectedRequest(BaseException):
    """A request the fake has no route for. BaseException on purpose: the bet
    loops catch Exception and retry forever, which would hang the test."""


class FakeTimeout(Exception):
    """Raised by FakeCasino.handle; adapters map it to the client's timeout."""


class FakeCasino:
    def __init__(self, balance: float = 10.0, currency: str = "usdt", script=()):
        self.balance = float(balance)
        self.currency = currency
        self.script = deque()
        self.calls = []        # (method, url, body) for every request
        self.bets = []         # {"outcome", "amount", "payout", "profit", "balance"}
        self.exhausted = False
        self.queue(*script)

    def queue(self, *outcomes):
        for o in outcomes:
            if o not in OUTCOMES:
                raise ValueError(f"unknown outcome {o!r}")
            self.script.append(o)

    def _path(self, url):
        for b in BASES:
            if url.startswith(b):
                return url[len(b):]
        raise UnexpectedRequest(f"unexpected host {url}")

    @property
    def bet_posts(self) -> int:
        return sum(1 for _, u, _ in self.calls if self._path(u) in (LIMBO, DICE))

    def gql_calls(self, op) -> int:
        return sum(1 for _, u, b in self.calls
                   if self._path(u) == GQL and b.get("operationName") == op)

    @property
    def profit(self) -> float:
        return sum(b["profit"] for b in self.bets)

    def handle(self, method: str, url: str, body: dict):
        """Return (status, json_body or str) or raise FakeTimeout."""
        self.calls.append((method, url, body))
        path = self._path(url)
        if method != "POST":
            raise UnexpectedRequest(f"unexpected request {method} {url}")
        if path == GQL:
            return 200, self._gql(body.get("operationName"))
        if path not in (LIMBO, DICE):
            raise UnexpectedRequest(f"unexpected request {method} {url}")

        amount = float(body["amount"])
        outcome = self.script.popleft() if self.script else None
        if outcome is None:
            self.exhausted = True
            outcome = "insufficient_balance"
        if outcome == "timeout":
            raise FakeTimeout()
        if outcome == "cf_403":
            return 403, CF_PAGE
        if outcome == "http_429":
            return 429, {"errors": [{"errorType": "rateLimit", "message": "Too many requests"}]}
        if outcome == "insufficient_balance" or amount > self.balance:
            return 400, {"errors": [{"errorType": "insufficientBalance",
                                     "message": "Insufficient balance"}]}

        win = outcome == "win"
        mult = self._payout_mult(path, body)
        payout = round(amount * mult, 8) if win else 0.0
        profit = payout - amount
        self.balance = round(self.balance + profit, 8)
        self.bets.append({"outcome": outcome, "amount": amount, "payout": payout,
                          "profit": profit, "balance": self.balance})
        return 200, self._bet_body(path, body, amount, payout, mult if win else 0.0, win)

    def _gql(self, op):
        if op == "AuthCheck":
            return {"data": {"user": {"id": "u1", "__typename": "User"}}}
        if op == "UserBalances":
            return {"data": {"user": {"id": "u1", "balances": [
                {"available": {"amount": self.balance, "currency": self.currency}}]}}}
        raise UnexpectedRequest(f"unexpected GraphQL operation {op}")

    @staticmethod
    def _payout_mult(path, body) -> float:
        if path == LIMBO:
            return float(body["multiplierTarget"])
        t = float(body["target"])
        chance = (100 - t) if body["condition"] == "above" else t
        return round(99.0 / chance, 4)

    def _bet_body(self, path, body, amount, payout, payout_mult, win):
        common = {"id": body["identifier"], "active": False, "amount": amount,
                  "payout": payout, "payoutMultiplier": payout_mult, "amountMultiplier": 1,
                  "currency": body["currency"]}
        if path == LIMBO:
            m = float(body["multiplierTarget"])
            res = round(m + 1.0, 4) if win else 1.0
            return {"limboBet": {**common, "game": "limbo", "result": res,
                                 "state": {"result": res, "multiplierTarget": m}}}
        t = float(body["target"])
        above = body["condition"] == "above"
        res = round(t + 1 if above == win else t - 1, 2)
        return {"diceRoll": {**common, "game": "dice", "result": res,
                             "state": {"result": res, "target": t,
                                       "condition": body["condition"]}}}

    # ── adapters ─────────────────────────────────────────
    def sync_session(self) -> "SyncSession":
        return SyncSession(self)

    def async_client(self) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content) if request.content else {}
            try:
                status, payload = self.handle(request.method, str(request.url), body)
            except FakeTimeout:
                raise httpx.ReadTimeout("fake timeout", request=request)
            if isinstance(payload, str):
                return httpx.Response(status, text=payload)
            return httpx.Response(status, json=payload)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class FakeResponse:
    def __init__(self, status: int, payload):
        self.status_code = status
        self._payload = payload
        self.text = payload if isinstance(payload, str) else json.dumps(payload)
        self.headers = {}

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("not JSON")
        return self._payload


class SyncSession:
    """Stands in for the CLI's module-level curl_cffi `_http` session."""

    def __init__(self, casino: FakeCasino):
        self.casino = casino

    def post(self, url, headers=None, json=None, timeout=None, **_):
        try:
            status, payload = self.casino.handle("POST", url, json or {})
        except FakeTimeout:
            raise CffiTimeout("fake timeout")
        return FakeResponse(status, payload)

    def close(self):
        pass

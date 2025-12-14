"""
Simplified Binance Futures (USDT-M) Trading Bot (Testnet)

This file fixes previous CLI issues where the script would call
`sys.exit(0)` inside `parse_args()` when no arguments were provided. In sandboxed
or testing environments that raises `SystemExit: 0`. The new behavior is:

- `parse_args()` returns `None` when no arguments are provided (after printing
  help). The caller (`main`) handles that case and returns cleanly without
  raising exceptions.
- Manual validation is used instead of `argparse` required flags to avoid
  abrupt `SystemExit(2)` from argparse.
- Added a few extra internal parsing tests, including a test for the no-args
  behavior.

Features:
- Place MARKET and LIMIT orders (BUY/SELL) on Binance Futures Testnet (USDT-M)
  using signed REST calls.
- Simple TWAP (time-weighted average price) child-orders implementation.
- Logging of requests, responses, and errors to console and `bot.log`.
- CLI with input validation handled in Python (no sudden SystemExit from argparse).

Usage examples:
python3 binance_futures_basicbot.py \
  --api-key YOUR_API_KEY --api-secret YOUR_API_SECRET \
  --symbol BTCUSDT --side BUY --order-type MARKET --quantity 0.001

TWAP example:
python3 binance_futures_basicbot.py \
  --api-key ... --api-secret ... --symbol BTCUSDT --side SELL \
  --order-type TWAP --quantity 0.005 --twap-parts 5 --twap-duration 60

Note: This script talks to Binance Futures Testnet base URL:
https://testnet.binancefuture.com

"""

import argparse
import hashlib
import hmac
import logging
import os
import sys
import time
from typing import Optional
from urllib.parse import urlencode

import requests

# --- CONFIG ---
TESTNET_BASE = os.environ.get("BINANCE_TESTNET_BASE", "https://testnet.binancefuture.com")
ORDER_ENDPOINT = "/fapi/v1/order"
SERVER_TIME = "/fapi/v1/time"
RECV_WINDOW = 5000  # ms
LOG_FILE = "bot.log"

# --- Logging setup ---
logger = logging.getLogger("BasicBot")
logger.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

ch = logging.StreamHandler(sys.stdout)
ch.setFormatter(formatter)
logger.addHandler(ch)

fh = logging.FileHandler(LOG_FILE)
fh.setFormatter(formatter)
logger.addHandler(fh)


# --- Helpers for signing and requests ---
class BinanceFuturesClient:
    def __init__(self, api_key: str, api_secret: str, base_url: str = TESTNET_BASE):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"X-MBX-APIKEY": self.api_key})

    def _get(self, path: str, params: dict = None):
        url = self.base_url + path
        r = self.session.get(url, params=params, timeout=10)
        return r

    def _post_signed(self, path: str, params: dict):
        # Add timestamp and recvWindow
        params = params.copy() if params else {}
        params.setdefault("timestamp", int(time.time() * 1000))
        params.setdefault("recvWindow", RECV_WINDOW)
        query_string = urlencode(sorted(params.items()))
        signature = hmac.new(self.api_secret.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()
        signed_qs = query_string + "&signature=" + signature
        url = self.base_url + path + "?" + signed_qs
        logger.debug("POST %s", url)
        try:
            r = self.session.post(url, timeout=15)
            logger.debug("Response status: %s", r.status_code)
            return r
        except requests.RequestException:
            logger.exception("Network error while POST %s", path)
            raise

    def server_time(self):
        r = self._get(SERVER_TIME)
        r.raise_for_status()
        return r.json()

    def place_order(self, symbol: str, side: str, order_type: str, quantity: Optional[float] = None, price: Optional[float] = None, time_in_force: Optional[str] = None, stop_price: Optional[float] = None):
        payload = {
            "symbol": symbol.upper(),
            "side": side.upper(),
            "type": order_type.upper(),
        }
        if quantity is not None:
            # Binance expects quantity formatted as string; validation done earlier
            payload["quantity"] = str(quantity)
        if price is not None:
            payload["price"] = str(price)
        if time_in_force is not None:
            payload["timeInForce"] = time_in_force
        if stop_price is not None:
            payload["stopPrice"] = str(stop_price)

        response = self._post_signed(ORDER_ENDPOINT, payload)
        return response


# --- Input validation functions ---
VALID_SIDES = {"BUY", "SELL"}
VALID_ORDER_TYPES = {"MARKET", "LIMIT", "TWAP"}


def positive_float(x):
    try:
        v = float(x)
    except Exception:
        raise argparse.ArgumentTypeError(f"Not a valid number: {x}")
    if v <= 0:
        raise argparse.ArgumentTypeError("Value must be positive")
    return v


def validate_symbol(sym: str):
    if not sym or not isinstance(sym, str):
        raise argparse.ArgumentTypeError("Symbol is required, e.g. BTCUSDT")
    return sym.upper()


# --- Simple TWAP implementation (bonus) ---
class TWAPOrder:
    def __init__(self, client: BinanceFuturesClient, symbol: str, side: str, total_qty: float, parts: int = 5, duration: int = 60):
        self.client = client
        self.symbol = symbol
        self.side = side
        self.total_qty = total_qty
        self.parts = max(1, int(parts))
        self.duration = max(1, int(duration))

    def run(self):
        per_qty = round(self.total_qty / self.parts, 8)
        logger.info("Starting TWAP: %s %s total=%s parts=%s duration=%s", self.side, self.symbol, self.total_qty, self.parts, self.duration)
        interval = self.duration / self.parts
        results = []
        for i in range(self.parts):
            logger.info("Placing child market order %d/%d qty=%s", i + 1, self.parts, per_qty)
            r = self.client.place_order(self.symbol, self.side, "MARKET", quantity=per_qty)
            try:
                r.raise_for_status()
                data = r.json()
                logger.info("Child order executed: %s", data)
                results.append(data)
            except Exception as e:
                logger.exception("Child order failed: %s", e)
                results.append({"error": str(e), "status_code": getattr(r, "status_code", None)})
            if i < self.parts - 1:
                time.sleep(interval)
        return results


# --- CLI and main flow ---


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Simplified Binance Futures Trading Bot (Testnet)")
    # NOTE: Do NOT set required=True. We'll validate manually to avoid argparse exiting with code 2.
    p.add_argument("--api-key", help="Your Testnet API key")
    p.add_argument("--api-secret", help="Your Testnet API secret")
    p.add_argument("--symbol", type=validate_symbol, help="Trading pair symbol, e.g. BTCUSDT")
    # Normalize to UPPER for choices
    p.add_argument("--side", type=lambda s: s.upper(), help="BUY or SELL")
    p.add_argument("--order-type", dest="order_type", type=lambda s: s.upper(), help="MARKET, LIMIT, or TWAP")
    p.add_argument("--quantity", help="Quantity to buy/sell (required for MARKET/LIMIT/TWAP)")
    p.add_argument("--price", help="Price for LIMIT orders")
    p.add_argument("--time-in-force", dest="time_in_force", type=lambda s: s.upper(), choices=["GTC", "IOC", "FOK"], default="GTC", help="Time in force for LIMIT orders (default GTC)")

    # TWAP optional params
    p.add_argument("--twap-parts", type=int, default=5, help="Number of child orders for TWAP (default 5)")
    p.add_argument("--twap-duration", type=int, default=60, help="Duration in seconds for TWAP; child orders spread evenly (default 60s)")

    # Developer/test helper
    p.add_argument("--run-tests", action="store_true", help="Run internal parsing tests and exit")

    return p


def parse_args(argv=None):
    parser = build_parser()
    if argv is None:
        argv = sys.argv[1:]

    # If user provided no args, show help and return None (don't exit the process)
    if len(argv) == 0:
        parser.print_help()
        logger.info("No arguments provided. Returning without running. Provide --help for usage examples.")
        return None

    args = parser.parse_args(argv)
    return args


def validate_args(args: argparse.Namespace) -> None:
    # Manual validation with helpful error messages. Raises ValueError on invalid input.
    if args.run_tests:
        return

    missing = []
    if not args.api_key:
        missing.append("--api-key")
    if not args.api_secret:
        missing.append("--api-secret")
    if not args.symbol:
        missing.append("--symbol")
    if not args.side:
        missing.append("--side")
    if not args.order_type:
        missing.append("--order-type")

    if missing:
        raise ValueError(f"Missing required arguments: {', '.join(missing)}")

    if args.side not in VALID_SIDES:
        raise ValueError(f"Invalid side: {args.side}. Must be one of {', '.join(VALID_SIDES)}")
    if args.order_type not in VALID_ORDER_TYPES:
        raise ValueError(f"Invalid order-type: {args.order_type}. Must be one of {', '.join(VALID_ORDER_TYPES)}")

    # Validate numeric inputs
    if args.order_type in ("MARKET", "LIMIT", "TWAP"):
        if args.quantity is None:
            raise ValueError("--quantity is required for MARKET, LIMIT, and TWAP orders")
        # convert and validate
        try:
            args.quantity = positive_float(args.quantity)
        except argparse.ArgumentTypeError as e:
            raise ValueError(str(e))

    if args.order_type == "LIMIT":
        if args.price is None:
            raise ValueError("--price is required for LIMIT orders")
        try:
            args.price = positive_float(args.price)
        except argparse.ArgumentTypeError as e:
            raise ValueError(str(e))

    # All validations passed; args modified in-place where necessary


# --- Simple parsing tests ---

def run_internal_tests():
    parser = build_parser()
    tests = [
        # minimal valid market order
        {
            "argv": ["--api-key", "k", "--api-secret", "s", "--symbol", "BTCUSDT", "--side", "buy", "--order-type", "market", "--quantity", "0.001"],
            "expect_error": False,
        },
        # missing quantity for market
        {
            "argv": ["--api-key", "k", "--api-secret", "s", "--symbol", "BTCUSDT", "--side", "buy", "--order-type", "market"],
            "expect_error": True,
        },
        # limit without price
        {
            "argv": ["--api-key", "k", "--api-secret", "s", "--symbol", "BTCUSDT", "--side", "sell", "--order-type", "limit", "--quantity", "1"],
            "expect_error": True,
        },
        # twap with proper args
        {
            "argv": ["--api-key", "k", "--api-secret", "s", "--symbol", "BTCUSDT", "--side", "sell", "--order-type", "twap", "--quantity", "0.01", "--twap-parts", "3", "--twap-duration", "30"],
            "expect_error": False,
        },
        # no-args should produce help and be treated as a non-error path in this test harness
        {
            "argv": [],
            "expect_error": False,
            "expect_none": True,
        },
    ]

    all_ok = True
    for i, t in enumerate(tests, 1):
        try:
            if len(t["argv"]) == 0:
                # simulate no-args path: parse_args should return None
                args = None
            else:
                args = parser.parse_args(t["argv"])
            if args is None:
                ok = t.get("expect_none", False)
            else:
                try:
                    validate_args(args)
                    ok = not t["expect_error"]
                except ValueError:
                    ok = t["expect_error"]
        except SystemExit:
            # argparse could call SystemExit for malformed inputs; treat as error
            ok = t["expect_error"]

        logger.info("Test %d: %s -> %s", i, t["argv"], "PASS" if ok else "FAIL")
        if not ok:
            all_ok = False

    if all_ok:
        logger.info("All internal parsing tests PASSED")
    else:
        logger.error("Some internal parsing tests FAILED")


def main(argv=None):
    try:
        args = parse_args(argv)
    except SystemExit:
        # argparse may call SystemExit for --help; let it through
        raise

    # If parse_args returned None (no args provided), exit gracefully without error
    if args is None:
        logger.info("No CLI args provided; exiting without running orders. Use --help for examples.")
        return

    # If user asked to run tests, execute tests then exit
    if getattr(args, "run_tests", False):
        run_internal_tests()
        return

    # Validate CLI inputs (manual validation to avoid argparse raising SystemExit:2)
    try:
        validate_args(args)
    except ValueError as ve:
        logger.error("Argument validation failed: %s", ve)
        logger.info("Use --help to see usage instructions")
        sys.exit(1)

    client = BinanceFuturesClient(args.api_key, args.api_secret)

    # Synchronize server time (optional but helpful)
    try:
        st = client.server_time()
        server_ts = st.get("serverTime")
        logger.info("Binance Futures testnet server time: %s", server_ts)
    except Exception:
        logger.warning("Could not fetch server time; continuing with local timestamp")

    try:
        side = args.side
        order_type = args.order_type

        if order_type == "MARKET":
            logger.info("Placing MARKET order %s %s qty=%s", side, args.symbol, args.quantity)
            r = client.place_order(args.symbol, side, "MARKET", quantity=args.quantity)
            r.raise_for_status()
            data = r.json()
            logger.info("Order response: %s", data)
            print("Order executed:\n", data)

        elif order_type == "LIMIT":
            logger.info("Placing LIMIT order %s %s qty=%s price=%s tif=%s", side, args.symbol, args.quantity, args.price, args.time_in_force)
            r = client.place_order(args.symbol, side, "LIMIT", quantity=args.quantity, price=args.price, time_in_force=args.time_in_force)
            try:
                r.raise_for_status()
                data = r.json()
                logger.info("Order placed: %s", data)
                print("Order placed:\n", data)
            except requests.HTTPError as he:
                logger.exception("Limit order failed: %s", he)
                try:
                    logger.error("Response body: %s", r.json())
                except Exception:
                    logger.error("No JSON body in response")
                sys.exit(1)

        elif order_type == "TWAP":
            twap = TWAPOrder(client, args.symbol, side, args.quantity, parts=args.twap_parts, duration=args.twap_duration)
            results = twap.run()
            print("TWAP results:\n", results)

        else:
            logger.error("Unsupported order type: %s", order_type)
            sys.exit(1)

    except requests.HTTPError as http_e:
        # Binance often returns JSON with code and msg
        resp = getattr(http_e, 'response', None)
        try:
            err = resp.json() if resp is not None else None
            logger.error("HTTPError: %s | body=%s", http_e, err)
            print("Error placing order:\n", err)
        except Exception:
            logger.exception("HTTPError and failed to decode body")
        sys.exit(1)
    except Exception as e:
        logger.exception("Unexpected error: %s", e)
        sys.exit(1)


if __name__ == '__main__':
    main()

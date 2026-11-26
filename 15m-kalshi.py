from decimal import Decimal, InvalidOperation, ROUND_UP
from uuid import uuid4
import base64
import datetime as dt
import math
import re
import statistics
import threading
import tkinter as tk
import random
from tkinter import ttk, messagebox, filedialog
from urllib.parse import urlparse
import json
from pathlib import Path
from urllib.parse import urlparse, urlencode
from arch import arch_model

from PIL import Image, ImageTk

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import numpy as np

from datetime import datetime, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DEMO_URL = 'https://external-api.demo.kalshi.co/trade-api/v2'
PRODUCTION_URL = 'https://external-api.kalshi.com/trade-api/v2'
COINBASE_URL = 'https://api.exchange.coinbase.com'

KELLY_FRACTION = 0.25
DEFAULT_FEE_CENTS = '0'

project_root = Path(__file__).resolve().parent

#SETTINGS_FILE = project_root / "files/.kellykalshi_settings.json"
#LAST_ORDER_FILE = project_root / "files/.kellykalshi_exit_state.json"

SETTINGS_FILE = Path.home() / ".kellykalshi_settings.json"
LAST_ORDER_FILE = Path.home() / ".kellykalshi_exit_state.json"

KELLY_QUARTER_FRACTION = 0.25

MONITOR_STATUS_1 = "MONITORING MARKET TO BUY"
MONITOR_STATUS_2 = "MONITORING MARKET TO SELL"

FILL_OR_KILL = "fill_or_kill"
GOOD_TILL_CANCELED = "good_till_canceled"
IMMEDIATE_OR_CANCEL = "immediate_or_cancel"

SUPPORTED_CRYPTO = {
    'BTC': {'name': 'Bitcoin', 'product': 'BTC-USD'},
    'ETH': {'name': 'Ethereum', 'product': 'ETH-USD'},
    'SOL': {'name': 'Solana', 'product': 'SOL-USD'},
    'XRP': {'name': 'XRP', 'product': 'XRP-USD'},
}

def utc_datetime(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone(dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def market_price_cents(market, side):
    """Only executable ask quotes: never substitute last trade for an ask."""
    value = market.get(f'{side}_ask_dollars')
    if value not in (None, ''):
        return float(value) * 100
    value = market.get(f'{side}_ask')
    if value not in (None, ''):
        return float(value)
    return None

class API:
    def __init__(self, key_id, private_key_path, environment):
        if not key_id or not private_key_path:
            raise ValueError('Enter your Kalshi API Key ID and PEM path.')
        self.key_id = key_id
        with open(private_key_path, 'rb') as file:
            self.private_key = serialization.load_pem_private_key(file.read(), password=None)
        self.base_url = DEMO_URL if environment == 'Demo' else PRODUCTION_URL

    def create_signature(self, timestamp, method, path):
        message = f"{timestamp}{method}{path.split('?')[0]}".encode()
        if isinstance(self.private_key, Ed25519PrivateKey):
            signature = self.private_key.sign(message)
        else:
            signature = self.private_key.sign(message, padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
        return base64.b64encode(signature).decode()

    def get(self, path):
        return self.request("GET", path)

    def request(self, method, path, data=None):
        timestamp = str(int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000))
        sign_path = urlparse(self.base_url + path).path
        headers = {
            'KALSHI-ACCESS-KEY': self.key_id,
            'KALSHI-ACCESS-SIGNATURE': self.create_signature(timestamp, method, sign_path),
            'KALSHI-ACCESS-TIMESTAMP': timestamp
        }
        response = requests.request(
            method, self.base_url + path, headers=headers, json=data, timeout=15
        )
        if not response.ok:
            raise ValueError(f"Kalshi returned HTTP {response.status_code}: {response.text}")
        return response.json()

    def make_order(self, ticker, side, count, price, order_id, time_in_force, reduce_op):
        yes_price = price if side == "yes" else Decimal("1") - price
        return self.request("POST", "/portfolio/events/orders", {
            "ticker": ticker,
            "side": "bid" if side == "yes" else "ask",
            "count": f"{count:.2f}",
            "price": f"{yes_price:.4f}",
            "time_in_force": time_in_force,
            "reduce_only": reduce_op,
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": order_id,
        })

    def transfer_between_indexes(self, source, destination, dollars, transfer_id):
        return self.request("POST", "/portfolio/intra_exchange_instance_transfer", {
            "amount": int(dollars * 10000),
            "source": "event_contract",
            "destination": "event_contract",
            "source_exchange_shard": source,
            "destination_exchange_shard": destination,
            "source_subaccount": 0,
            "destination_subaccount": 0,
            "client_transfer_id": transfer_id,
        })

    def markets(self, ticker):
        markets = []
        cursor = None
        for _ in range(10):
            params = {'series_ticker': ticker.upper(), 'status': 'open', 'limit': 200}
            if cursor:
                params['cursor'] = cursor
            response = requests.get(self.base_url + '/markets', params=params, timeout=15)
            response.raise_for_status()
            data = response.json()
            markets.extend(data.get('markets', []))
            cursor = data.get('cursor')
            if not cursor:
                break
        return {'markets': markets}

    def market(self, ticker):
        response = requests.get(self.base_url + '/markets/' + ticker, timeout=15)
        response.raise_for_status()
        return response.json()['market']

    def get_pages(self, path, collection, params=None):
        params = dict(params or {})
        result = []
        cursor = None
        for _ in range(100):
            query = dict(params)
            query['limit'] = 200
            if cursor:
                query['cursor'] = cursor
            data = self.get(path + '?' + urlencode(query))
            result.extend(data.get(collection, []))
            cursor = data.get('cursor')
            if not cursor:
                return result
        raise RuntimeError('Pagination limit exceeded; cannot safely verify exchange history.')

    def executable_bid(self, ticker, side):
        data = self.get('/markets/' + ticker + '/orderbook')
        book = data.get('orderbook_fp', {})
        rows = book.get('yes_dollars' if side == 'yes' else 'no_dollars', [])
        if not rows:
            return None, Decimal('0')
        price = max(Decimal(str(row[0])) for row in rows)
        qty = sum((Decimal(str(row[1])) for row in rows if Decimal(str(row[0])) == price), Decimal('0'))
        return price, qty

class MarketData:
    def identify_crypto(self, market):
        ticker = str(market.get('ticker') or '').upper()
        series = str(market.get('series_ticker') or '').upper()
        identifiers = (ticker, series)
        from_ids = {symbol for symbol in SUPPORTED_CRYPTO
                    if any(value.startswith('KX' + symbol) for value in identifiers)}
        if len(from_ids) > 1:
            raise ValueError('Conflicting cryptocurrency identifiers; no estimate generated.')
        if len(from_ids) == 1:
            return next(iter(from_ids))

        title = str(market.get('title') or '')
        patterns = {
            'BTC': r'\b(?:BTC|Bitcoin)\b',
            'ETH': r'\b(?:ETH|Ethereum|Ether)\b',
            'SOL': r'\b(?:SOL|Solana)\b',
            'XRP': r'\b(?:XRP|Ripple)\b',
        }
        found = {symbol for symbol, pattern in patterns.items()
                if re.search(pattern, title, re.IGNORECASE)}
        if len(found) != 1:
            raise ValueError('Only unambiguous BTC, ETH and SOL price markets are supported; no estimate generated.')
        return next(iter(found))


    def extract_crypto_threshold(self, market):
        title = ' '.join(str(market.get(k) or '') for k in
                        ('title', 'subtitle', 'yes_sub_title', 'rules_primary')).lower()
        above = bool(re.search(r'\b(?:above|over|greater than|at least|or higher|or more)\b', title))
        below = bool(re.search(r'\b(?:below|under|less than|at most|or lower)\b', title))
        if above == below:
            raise ValueError('Cannot identify an unambiguous ABOVE/BELOW threshold. Dynamic UP/DOWN contracts need the official reference price and settlement feed.')
        direction = 'above' if above else 'below'
        strike = market.get('floor_strike') if above else market.get('cap_strike')
        if strike in (None, ''):
            raise ValueError('No explicit numeric floor_strike/cap_strike; no estimate generated.')
        strike = float(strike)
        if not math.isfinite(strike) or strike <= 0:
            raise ValueError('Invalid cryptocurrency USD threshold; no estimate generated.')
        return direction, strike

    def crypto_statistics(self, market):
        symbol = self.identify_crypto(market)
        product = SUPPORTED_CRYPTO[symbol]['product']
        direction, strike = self.extract_crypto_threshold(market)
        close = utc_datetime(market.get('close_time'))
        now = dt.datetime.now(dt.timezone.utc)
        if close is None or close <= now:
            raise ValueError('Market close time is missing or already passed.')
        minutes_left = (close - now).total_seconds() / 60
        if minutes_left > 24 * 60:
            raise ValueError('This short-horizon crypto model only supports markets closing within 24 hours.')

        ticker_response = requests.get(f'{COINBASE_URL}/products/{product}/ticker', timeout=15)
        ticker_response.raise_for_status()
        spot = float(ticker_response.json()['price'])
        if not math.isfinite(spot) or spot <= 0:
            raise ValueError('Invalid Coinbase spot price.')
        candles_response = requests.get(f'{COINBASE_URL}/products/{product}/candles',
                                        params={'granularity': 60}, timeout=15)
        candles_response.raise_for_status()
        candles = sorted(candles_response.json(), key=lambda row: row[0])
        candles = [c for c in candles if len(c) >= 5 and float(c[4]) > 0]
        current_minute = int(now.timestamp() // 60) * 60
        closes = [(int(c[0]), float(c[4])) for c in candles if int(c[0]) < current_minute]
        returns = [math.log(b[1] / a[1]) for a, b in zip(closes, closes[1:])
                if b[0] - a[0] == 60]
        if len(returns) < 60:
            raise ValueError(f'Not enough consecutive historical {symbol} minute candles to estimate volatility.')
        sigma_minute = statistics.stdev(returns)
        if not math.isfinite(sigma_minute) or sigma_minute <= 0:
            raise ValueError('Invalid measured volatility; cannot estimate probability.')
        z = math.log(strike / spot) / (sigma_minute * math.sqrt(minutes_left))
        cdf = 0.5 * (1 + math.erf(z / math.sqrt(2)))
        p_yes = 1 - cdf if direction == 'above' else cdf
        return {
            'symbol': symbol,
            'p_yes': min(1, max(0, p_yes)),
            'spot': spot,
            'strike': strike,
            'direction': direction,
            'minutes_left': minutes_left,
            'sigma_minute': sigma_minute,
            'sample_count': len(returns),
            'returns': returns,
            'source': f'Coinbase Exchange {product} ticker and 1-minute candles',
            'warning': (
                'Exploratory zero-drift lognormal model only. '
                'Coinbase prices may NOT match Kalshi settlement index.'
            )
        }

class KellyCalculator:
    def calculate(self, estimate, price_cents, fee_cents, balance, multiplier):
        e = float(estimate)
        f = float(fee_cents)

        if not 0 <= e <= 1:
            raise ValueError('Win probability percentage must be between 0 and 100%.')
        if price_cents is None or not 0 < price_cents < 100:
            raise ValueError('No executable ask quote is available.')
        if f < 0 or not math.isfinite(f):
            raise ValueError('Fee must be a nonnegative finite number.')
        if balance < 0:
            raise ValueError('Bankroll cannot be negative.')

        effective_cost = (price_cents + f) / 100
        if effective_cost >= 1:
            print('zz')
            raise ValueError('Cost including estimated fees must be below $1.')
        full_kelly = max(0, (e - effective_cost) / (1 - effective_cost))
        wager = balance * full_kelly * multiplier
        return wager

class MonteCarloEstimator:
    def __init__(self, simulations=20000):
        self.simulations = simulations
        self.market_database = MarketData()
        self.rng = np.random.default_rng()

    def estimate(self, market, side="yes"):
        if side not in ("yes", "no"):
            raise ValueError("Invalid side.")
        stats = self.market_database.crypto_statistics(market)
        spot = stats["spot"]
        strike = stats["strike"]
        direction = stats["direction"]
        minutes_left = stats["minutes_left"]
        returns = np.array(stats["returns"], dtype=float)
        # Number of complete minutes remaining
        full_minutes = int(minutes_left)

        # Fraction of a minute remaining
        fraction = minutes_left - full_minutes

        # Store cumulative returns for each simulation
        cumulative_returns = np.zeros(self.simulations)

        # Randomly sample actual historical returns
        for _ in range(full_minutes):

            sampled_returns = self.rng.choice(
                returns,
                size=self.simulations,
                replace=True
            )

            cumulative_returns += sampled_returns

        # Account for remaining seconds
        if fraction > 0:

            sampled_returns = self.rng.choice(
                returns,
                size=self.simulations,
                replace=True
            )

            cumulative_returns += (
                sampled_returns * np.sqrt(fraction)
            )

        # Calculate final simulated prices
        final_prices = spot * np.exp(cumulative_returns)

        # Calculate YES probability
        if direction == "above":
            p_yes = np.mean(final_prices > strike)

        elif direction == "below":
            p_yes = np.mean(final_prices < strike)

        else:
            raise ValueError("Invalid market direction.")

        if side == "yes":
            return float(p_yes)

        return float(1 - p_yes)

class ChanceEstimator:
    def estimate(self, market, side):
        market_database = MarketData()
        stats = market_database.crypto_statistics(market)
        p = stats['p_yes'] if side == 'yes' else 1 - stats['p_yes']
        return p

class GARCHEstimator:

    def __init__(self, simulations=30000):
        self.simulations = simulations
        self.market_database = MarketData()
        self.rng = np.random.default_rng()

    def estimate(self, market, side="yes"):

        if side not in ("yes", "no"):
            raise ValueError("Invalid side.")

        stats = self.market_database.crypto_statistics(market)

        spot = float(stats["spot"])
        strike = float(stats["strike"])
        direction = stats["direction"]
        minutes_left = float(stats["minutes_left"])

        returns = np.array(stats["returns"], dtype=float)

        # Convert returns into percentage units
        returns = returns * 100

        if len(returns) < 200:
            raise ValueError("Insufficient historical returns for GARCH.")

        # Fit GARCH(1,1) with Student-t innovations
        model = arch_model(
            returns,
            mean="Zero",
            vol="GARCH",
            p=1,
            q=1,
            dist="StudentsT",
            rescale=False
        )

        fitted = model.fit(disp="off")

        if fitted.convergence_flag != 0:
            raise ValueError("GARCH model failed to converge.")

        params = fitted.params

        omega = float(params["omega"])
        alpha = float(params["alpha[1]"])
        beta = float(params["beta[1]"])
        nu = float(params["nu"])

        if nu <= 2:
            raise ValueError("Invalid Student-t degrees of freedom.")

        # Forecast next-minute variance
        forecast = fitted.forecast(horizon=1)
        initial_variance = float(
            forecast.variance.iloc[-1, 0]
        )

        if not np.isfinite(initial_variance) or initial_variance <= 0:
            raise ValueError("Invalid forecast variance.")

        variance = np.full(
            self.simulations,
            initial_variance
        )

        log_prices = np.full(
            self.simulations,
            np.log(spot)
        )

        full_minutes = int(minutes_left)
        fraction = minutes_left - full_minutes

        # Standardize Student-t innovations
        scale = np.sqrt((nu - 2) / nu)

        for _ in range(full_minutes):

            shocks = self.rng.standard_t(
                nu,
                size=self.simulations
            ) * scale

            simulated_returns = (
                np.sqrt(variance) * shocks
            )

            log_prices += simulated_returns / 100

            # Update volatility separately for every path
            variance = (
                omega
                + alpha * simulated_returns ** 2
                + beta * variance
            )

            variance = np.maximum(variance, 1e-12)

        # Simulate remaining fractional minute
        if fraction > 0:

            shocks = self.rng.standard_t(
                nu,
                size=self.simulations
            ) * scale

            simulated_returns = (
                np.sqrt(variance * fraction) * shocks
            )

            log_prices += simulated_returns / 100

        final_prices = np.exp(log_prices)

        if not np.all(np.isfinite(final_prices)):
            raise ValueError("Invalid simulated prices.")

        if direction == "above":
            wins = np.sum(final_prices > strike)

        elif direction == "below":
            wins = np.sum(final_prices < strike)

        else:
            raise ValueError("Invalid market direction.")

        # Avoid returning absolute 0% or 100%
        # solely because of finite simulation counts
        p_yes = (wins + 0.5) / (self.simulations + 1)

        return float(
            p_yes if side == "yes" else 1 - p_yes
        )

class ForecastEstimator:
    def __init__(self, simulations=20000):
        self.simulations = simulations
        self.market_database = MarketData()
        self.rng = np.random.default_rng()

    def estimate(self, market, side, minutes_ahead):
        if side not in ("yes", "no"):
            raise ValueError("Invalid side.")

        stats = self.market_database.crypto_statistics(market)

        spot = stats["spot"]
        strike = stats["strike"]
        direction = stats["direction"]
        minutes_left = stats["minutes_left"]
        sigma = stats["sigma_minute"]

        returns = np.array(stats["returns"], dtype=float)

        if not 0 < minutes_ahead < minutes_left:
            raise ValueError("Invalid forecasting horizon.")

        remaining = minutes_left - minutes_ahead

        cumulative_returns = np.zeros(self.simulations)

        # Simulate price movements over the next 5 minutes
        for _ in range(int(minutes_ahead)):
            cumulative_returns += self.rng.choice(
                returns,
                size=self.simulations,
                replace=True
            )

        fraction = minutes_ahead % 1

        if fraction > 0:
            cumulative_returns += self.rng.choice(
                returns,
                size=self.simulations,
                replace=True
            ) * np.sqrt(fraction)

        future_prices = spot * np.exp(cumulative_returns)

        # Calculate win probability at each simulated future price
        z = np.log(strike / future_prices) / (
            sigma * np.sqrt(remaining)
        )

        cdf = np.array([
            0.5 * (1 + math.erf(float(v) / math.sqrt(2)))
            for v in z
        ])

        probabilities = 1 - cdf if direction == "above" else cdf

        if side == "no":
            probabilities = 1 - probabilities

        return float(np.mean(probabilities))
    
class LoopApp:
    def __init__(self, root):
        self.root = root
        self.root.title('Crypto 15M Market Kalshi Bot')
        self.root.state('zoomed')
        self.root.minsize(680, 450)    
        self.loop_thread = threading.Event() 
        self.api = None
        self.loop_index = 0
        self.loop_running = False

        self.last_order_market_ticker = ""
        self.last_order_side = ""
        self.last_order_count = 0
        self.last_order_price = 0
        self.last_order_id = ""

        self.balance = 0
        self.last_percent_change = None

        self.build_ui()
        self.load_settings()
        self.load_last_order()

    def run_worker(self, job, success, error=None):
        def worker():
            try:
                result = job()
            except Exception as exc:
                self.root.after(0, lambda e=str(exc):
                                (error(e) if error else self.send_error(e)))
            else:
                self.root.after(0, lambda result=result: success(result))
        threading.Thread(target=worker, daemon=True).start()

    def build_ui(self):
        self.build_layout()
        self.build_test()
        self.build_text_box()
        self.build_auth()
        self.build_kelly_calculator()
        self.build_market_view()
        self.build_position_monitor()
        self.build_last_order_view()
        self.build_other_settings()
        self.print_cat()

    def build_layout(self):
        self.paned_window = ttk.PanedWindow(root, orient="horizontal")
        self.paned_window.pack(fill="both", expand=True, padx=5, pady=5)
        self.panel1 = tk.Frame(self.paned_window)
        self.panel2 = tk.Frame(self.paned_window)
        self.paned_window.add(self.panel1, weight=2)
        self.paned_window.add(self.panel2, weight=1) 

    def build_test(self):
        frame = ttk.LabelFrame(self.panel1, text='Execution settings', padding=10)
        frame.pack(fill='x', pady=5)
        row = ttk.Frame(frame); row.pack(fill='x', pady=(6, 2))
        self.execute_loop_button = ttk.Button(row, text='Execute', command=self.start_test_loop)
        self.execute_loop_button.pack(side='left')
        #ttk.Label(row, text='Test label').pack(side='left', padx=12)
        ttk.Label(row, text='Run every: ').pack(side='left', padx=12)
        self.sec_entry = ttk.Entry(
                row,
                width=10,
            )
        self.sec_entry.pack(side='left', fill='x', expand=False)
        self.sec_entry.insert(0, '5')
        ttk.Label(row, text='seconds').pack(side='left', padx=12)
        ttk.Label(row, text='Balance minimum').pack(side='left')
        self.bal_min_entry = ttk.Entry(row, width=7)
        self.bal_min_entry.pack(side='left', padx=5)
        ttk.Label(row, text='Balance maximum').pack(side='left')
        self.bal_max_entry = ttk.Entry(row, width=7)
        self.bal_max_entry.pack(side='left', padx=5)

        self.monitor_side = tk.StringVar(value='buy')
        self.button_one = ttk.Radiobutton(row, text='BUY', variable=self.monitor_side, value='buy',
                        command=None)
        self.button_one.pack(side='left', padx=5)
        self.button_two = ttk.Radiobutton(row, text='SELL', variable=self.monitor_side, value='sell',
                        command=None)
        self.button_two.pack(side='left', padx=5)

    def start_test_loop(self):
        self.save_settings()

        if not self.api:
            self.send_error('Error: ', 'Not connected to API.')
        try:
            ticker, multiplier, tp, sl, api, bal_min, bal_max = self.get_loop_requirements()
        except Exception as exc:
            self.send_error('Error: ', exc)

        if self.loop_running == True:
            self.insert_request_text('loop running, stopping')
            self.loop_thread.set()
            self.loop_running = False
            self.reenable_all_buttons()
            self.execute_loop_button.config(text="Execute")
            return

        interval = Decimal(5.0)

        try: 
            interval = Decimal(self.sec_entry.get().strip())
        except:
            self.insert_request_text('Interval entered invalid, 5.0 automatically set.')

        self.loop_thread.clear()
        self.loop_running = True
        self.disable_all_buttons()
        self.execute_loop_button.config(text="Abort")

        def worker():
            try:
                self.test_loop(interval=interval, series_ticker=ticker, multiplier=multiplier, tp=tp, sl=sl, api=api, bal_min=bal_min, bal_max=bal_max)
            except Exception as exc:
                self.send_error('Error: ', exc)          
            finally:
                self.loop_index = 0
                self.loop_thread.set()
                self.reenable_all_buttons()
                self.execute_loop_button.config(text="Execute")
                self.exit_running = False
                self.root.after(0, lambda: None)
        threading.Thread(target=worker, daemon=True).start()

    def disable_all_buttons(self):
        self.sec_entry.configure(state='disabled')
        self.private_key_path_entry.configure(state='disabled')
        self.api_key_entry.configure(state='disabled')
        self.connect_button.configure(state='disabled')
        self.browse_button.configure(state='disabled')
        self.environment_combo_box.configure(state='disabled')
        self.kelly_fraction_combo.configure(state='disabled')
        self.series_ticker_entry_combo.configure(state='disabled')
        self.tp_entry.configure(state='disabled')
        self.sl_entry.configure(state='disabled')
        self.bal_max_entry.configure(state='disabled')
        self.bal_min_entry.configure(state='disabled')
        self.bal_min_entry.configure(state='disabled')
        self.button_one.configure(state='disabled')
        self.button_two.configure(state='disabled')
        self.estimation_method_combo.configure(state='disabled')
        self.enable_market_switching_button.configure(state='disabled')
        

    def reenable_all_buttons(self):
        self.sec_entry.configure(state='normal')
        self.private_key_path_entry.configure(state='normal')
        self.api_key_entry.configure(state='normal')
        self.connect_button.configure(state='normal')
        self.browse_button.configure(state='normal')
        self.environment_combo_box.configure(state='normal')
        self.kelly_fraction_combo.configure(state='normal')
        self.series_ticker_entry_combo.configure(state='normal')
        self.tp_entry.configure(state='normal')
        self.sl_entry.configure(state='normal')
        self.bal_max_entry.configure(state='normal')
        self.bal_min_entry.configure(state='normal')
        self.button_one.configure(state='normal')
        self.button_two.configure(state='normal')
        self.estimation_method_combo.configure(state='normal')
        self.enable_market_switching_button.configure(state='normal')

    def test_loop(self, interval, series_ticker, multiplier, tp, sl, api, bal_min, bal_max):
        self.insert_request_text(f"Began execution with {multiplier:.2f} kelly wager multiplier, {series_ticker} TICKER, {(tp*100):.2f}% take profit, {(sl*100):.2f}% stop loss amount, ${bal_min:.2f} minimum, and ${bal_max:.2f} max.")

        ticker = series_ticker

        if self.monitor_side.get() == 'buy':
            loop_state = MONITOR_STATUS_1
        else:
            if self.load_last_order():
                loop_state = MONITOR_STATUS_2
                self.insert_request_text(f"Retrieved order with ticker {self.last_order_market_ticker} on side {self.last_order_side}. Proceeding to monitor to sell.")
            else:
                raise ValueError('Last order not found, cannot start with monitoring to sell.')

        while not self.loop_thread.wait(interval):
            self.insert_request_text(f'{loop_state} in {ticker}')

            try:
                check_balance = Decimal(self.balance)
                self.insert_request_text(f"Current balance received: ${check_balance:.2f}") 
            except (InvalidOperation, ValueError):
                raise ValueError('balance invalid')
            
            if (check_balance <= bal_min):
                self.insert_request_text(f"Balance {check_balance} falls under minimum of ${bal_min:.2f}, aborting.")
                return
            if (check_balance >= bal_max):
                self.insert_request_text(f"Balance {check_balance} above maximum of ${bal_max:.2f}, aborting.")
                return
            
            self.loop_index += 1

            try:
                self.update_on_loop(api)
            except Exception as exc:
                self.insert_request_text(str(exc))
                continue

            if loop_state == MONITOR_STATUS_1:
                try:
                    market_data, market_ticker = self.get_market_data_and_ticker(api, ticker)

                    minutes_remaining = self.get_minutes_remaining(market_data)
                    if (minutes_remaining < 3): 
                        #self.print_cat()
                        self.insert_request_text(f'{minutes_remaining:.2f} minutes left. Buying disabnled.')
                        choices = ["(˶ˆᗜˆ˵)", "o3<", "(｡· ⩊ ·｡) ?", "mrp", "mreow", "(˶>⩊<˶)", "GRRF", "WUF", "BLERK!", "HONLK", "ฅ(•ㅅ•❀)ฅ ", "( • ⩊ • ) ", "   ᓚ₍⑅^..^₎♡"]
                        random_item = random.choice(choices)
                        self.insert_request_text(f'{random_item}')
                        continue
                    self.validate_market_for_monitoring(api, market_ticker)
                    yes_estimate = self.get_side_win_estimates(market_data)
                    yes_price_cents = self.get_contract_price_cents(market_data)
                    wager, limit, side = self.get_preferred_side_wager(yes_estimate, yes_price_cents, self.balance, multiplier)
                    count, limit, fee = self.get_contract_count_limit_and_fee(wager, limit)
                    estimated_total = count * (limit + fee) 
                    self.verify_balance(api, market_ticker, estimated_total)
                    self.attempt_order(api, market_data, side, market_ticker, count, limit)
                except ChangeMarketError as ch:
                    enable_market_switching = self.enable_market_switching.get()
                    if not enable_market_switching: continue
                    minutes_remaining = self.get_minutes_remaining(market_data)
                    #self.print_cat()
                    if (minutes_remaining < 5): 
                        self.insert_request_text(f'Would switch markets but there are {minutes_remaining:.2f} minutes left sis.')
                        choices = ["o3o", "o3<", "hi", "mrp", "mreow", "maow"]
                        random_item = random.choice(choices)
                        self.insert_request_text(f'{random_item}')
                        choices_key = [1,2,3]
                        random_key = random.choice(choices_key)
                        if (random_key == 1):
                            self.print_cat()
                        elif (random_key == 2):
                            self.print_horse()
                        continue
                    
                    tickers = [
                        "KXBTC15M",
                        "KXETH15M",
                        "KXSOL15M",
                        "KXXRP15M"
                    ]

                    current_index = tickers.index(ticker)
                    next_index = (current_index + 1) % len(tickers)

                    ticker = tickers[next_index]

                    self.insert_request_text(f'Switching to {ticker}')
                    continue
                except Exception as exc:
                    self.insert_request_text(str(exc) + ' Restarting.')
                    continue

                self.insert_request_text('Switching loop state to monitor order...')
                loop_state = MONITOR_STATUS_2
                continue
            else:
                order_side = self.last_order_side
                market_ticker = self.last_order_market_ticker

                if market_ticker is None or order_side is None:
                    self.insert_request_text('No market ticker or order side found. Restarting loop at monitor for purchase state.')
                    loop_state = MONITOR_STATUS_1
                    continue

                try:
                    self.validate_market_for_positions(api, market_ticker)
                    _, second_state_market_ticker = self.get_market_data_and_ticker(api, ticker)
                    if second_state_market_ticker != market_ticker:
                        self.insert_request_text('Current market does not match market purchased order on. Restarting with MONITOR_STATUS_1.')
                        loop_state = MONITOR_STATUS_1
                        continue
                    self.attempt_sell(api,tp,sl,order_side,market_ticker)
                except MonitorStateError as mon:
                    self.insert_request_text(str(mon) + ' Restarting with MONITOR_STATUS_1.')
                    loop_state = MONITOR_STATUS_1
                    continue
                except Exception as exc:
                    self.insert_request_text(str(exc) + ' Restarting.')
                    continue

                continue

    def get_loop_requirements(self):
        api = self.api
        ticker = self.series_ticker_entry.get().strip()

        if not ticker:
            raise ValueError('Ticker not set.')

        kelly_fraction = self.kelly_fraction.get()
        if kelly_fraction == "Full (100%)":
            multiplier = 1.0

        elif kelly_fraction == "Half (50%)":
            multiplier = 0.5

        elif kelly_fraction == "Quarter (25%)":
            multiplier = 0.25

        try:
            tp = Decimal(self.tp_entry.get()) / 100
            sl = Decimal(self.sl_entry.get()) / 100

            if not all(v.is_finite() for v in (tp, sl)) or tp <= 0 or not 0 < sl < 1:
                raise ValueError('tp and sl invalid.')
        except (InvalidOperation, ValueError):
            raise ValueError('tp and sl invalid.')

        balance = Decimal(self.balance)

        try: 
            bal_min = Decimal(str(self.bal_min_entry.get().strip()))
            bal_max = Decimal(str(self.bal_max_entry.get().strip()))
        except (InvalidOperation, ValueError):
                raise ValueError('bal_min and bal_max invalid.')

        if not all(v.is_finite() for v in (bal_min, bal_max)) or not bal_min < balance or not bal_max > balance:
            raise ValueError('bal_min and bal_max invalid.')

        return ticker, multiplier, tp, sl, api, bal_min, bal_max
        
    def update_on_loop(self, api):
        self.refresh_balance(api)

    def build_text_box(self):
        frame = ttk.LabelFrame(self.panel2, text='Process text', padding=10)
        frame.pack(fill='x', pady=7)
        row = ttk.Frame(frame); row.pack(fill='x')
        self.app_text = tk.Text(row, height=70)
        self.app_text.pack(fill='both', expand=True)
    
    def set_request_text(self, text):
        self.app_text.configure(state='normal')
        self.app_text.delete('1.0', tk.END)
        self.app_text.insert('1.0', text)

    def insert_request_text(self, text):
        self.app_text.configure(state='normal')
        self.app_text.insert(1.0, f'[{dt.datetime.now(dt.timezone.utc)}] {text}\n')

    def build_auth(self):
        frame = ttk.LabelFrame(self.panel1, text='Kalshi API connection', padding=10)
        frame.pack(fill='x', pady=6)
        self.balance_label = ttk.Label(frame, text='Available balance: —', font=('Segoe UI', 14, 'bold'))
        self.balance_label.pack(anchor='w')

        row = ttk.Frame(frame); row.pack(fill='x', pady=5)
        self.last_updated_label = ttk.Label(row, text='Last updated: -')
        self.last_updated_label.pack(side='left')
        
        row = ttk.Frame(frame); row.pack(fill='x', pady=5)
        
        ttk.Label(row, text='Environment:', width=14).pack(side='left')
        self.environment = tk.StringVar(value='Production')
        self.environment_combo_box = ttk.Combobox(row, textvariable=self.environment, values=['Production', 'Demo'],state='readonly', width=15)
        self.environment_combo_box.pack(side='left')
        row = ttk.Frame(frame); row.pack(fill='x', pady=3)
        ttk.Label(row, text='API Key ID:', width=14).pack(side='left')
        self.api_key_entry = ttk.Entry(row,width=30,show='*')
        self.api_key_entry.pack(side='left', fill='x', expand=False)
        row = ttk.Frame(frame); row.pack(fill='x', pady=3)
        ttk.Label(row, text='PEM Path:', width=14).pack(side='left')
        self.private_key_path_entry = ttk.Entry(row, width=30)
        self.private_key_path_entry.pack(side='left', fill='x', expand=False)
        self.browse_button = ttk.Button(row, text="Browse…", command=self.browse_pem)
        self.browse_button.pack(side="left", padx=(6, 0))
        row = ttk.Frame(frame); row.pack(fill='x', pady=(6, 2))
        self.connect_button = ttk.Button(row, text='Connect', command=self.connect_to_api)
        self.connect_button.pack(side='left')
        self.api_connection_label = ttk.Label(row, text='Not connected', foreground='chocolate1')
        self.api_connection_label.pack(side='left', padx=12)

    def connect_to_api(self):
        api_key = self.api_key_entry.get().strip()
        private_path = self.private_key_path_entry.get().strip()
        env = self.environment.get()

        self.api_connection_label.config(text='Connecting…')
        def job():
            api = API(api_key, private_path, env)
            self.refresh_balance(api)
            return api
        def success(result):
            self.api = result
            self.api_connection_label.config(text='Connected')
            self.insert_request_text('Connected to API.')
        self.run_worker(job, success,
                        lambda error: (self.api_connection_label.config(text='Not connected'),
                                        self.send_error('Error: ', error)))

    def refresh_balance(self, api):
        balance = api.get('/portfolio/balance')['balance'] / 100
        if not balance:
            raise ValueError('Balance cannot be retrieved')
        self.balance_label.config(text=f'Available balance: ${balance:.2f}')
        self.last_updated_label.config(text=f'Last updated (UTC): {dt.datetime.now(dt.timezone.utc)}')
        self.balance = balance

    def save_settings(self):
            settings = {
                "kalshi_api_key": self.api_key_entry.get().strip(),
                "pem_path": self.private_key_path_entry.get().strip(),
                "environment": self.environment.get(),
                "kelly_fraction": self.kelly_fraction.get(),
                "min_bal": str(self.bal_min_entry.get().strip()),
                "max_bal": str(self.bal_max_entry.get().strip()),
                "tp_entry": str(self.tp_entry.get().strip()),
                "sl_entry": str(self.sl_entry.get().strip()),
                "enable_market_switching": self.enable_market_switching.get(),
                "estimation_method": str(self.estimation_method.get()),
                "buy_or_sell": str(self.monitor_side.get()),
                "series_ticker": str(self.series_ticker_entry.get())
            }
    
            with open(SETTINGS_FILE, "w", encoding="utf-8") as file:
                json.dump(settings, file, indent=4)
    
    
    def load_settings(self):
        if not SETTINGS_FILE.exists():
            return

        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as file:
                settings = json.load(file)

            self.bal_min_entry.delete(0, tk.END)
            self.bal_min_entry.insert(
                0, settings.get("min_bal", "")
            )

            self.bal_max_entry.delete(0, tk.END)
            self.bal_max_entry.insert(
                0, settings.get("max_bal", "")
            )

            self.tp_entry.delete(0, tk.END)
            self.tp_entry.insert(
                0, settings.get("tp_entry", "")
            )

            self.sl_entry.delete(0, tk.END)
            self.sl_entry.insert(
                0, settings.get("sl_entry", "")
            )

            series_ticker = settings.get("series_ticker")
            if series_ticker in ("KXBTC15M",
                                    "KXETH15M",
                                    "KXSOL15M",
                                    "KXXRP15M"):
                self.series_ticker_entry.set(series_ticker)

            environment = settings.get("environment")
            if environment in ("Demo", "Production"):
                self.environment.set(environment)

            self.enable_market_switching.set(settings.get("enable_market_switching", False))

            self.monitor_side.set(settings.get("buy_or_sell", 'buy'))

            estimation_method = settings.get("estimation_method")
            if estimation_method in ("Standard Logistical", "Monte Carlo", "Garche"):
                self.estimation_method.set(estimation_method)

            kelly_fraction = settings.get("kelly_fraction")
            if kelly_fraction in ("Quarter (25%)", "Half (50%)", "Full (100%)"):
                self.kelly_fraction.set(kelly_fraction)

            self.api_key_entry.delete(0, tk.END)
            self.api_key_entry.insert(
                0, settings.get("kalshi_api_key", "")
            )

            self.private_key_path_entry.delete(0, tk.END)
            self.private_key_path_entry.insert(
                0, settings.get("pem_path", "")
            )

        except (OSError, ValueError) as exc:
            self.send_error('Error: ',exc)

    def send_error(self, head, text):
        self.insert_request_text(text)
        messagebox.showinfo(head, text)
        raise ValueError(text)

    def build_kelly_calculator(self):
        frame = ttk.LabelFrame(self.panel1, text='Kelly Criterion', padding=10)
        frame.pack(fill='x', pady=9)

        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(6, 2))
        ttk.Label(row, text="Kelly Fraction:", width=14).pack(side="left")
        self.kelly_fraction = tk.StringVar(value="Quarter (25%)")
        self.kelly_fraction_combo = ttk.Combobox(
        row,
        textvariable=self.kelly_fraction,
        values=[
            "Full (100%)",
            "Half (50%)",
            "Quarter (25%)",
        ],
        state="readonly",
        width=18
        )
        self.kelly_fraction_combo.pack(side="left")

    def build_market_view(self):
        frame = ttk.LabelFrame(self.panel1, text='Create market orders', padding=10)
        frame.pack(fill='x', pady=10)

        row = ttk.Frame(frame); row.pack(fill='x')

        ttk.Label(row, text='Series ticker:', width = 14).pack(side='left')
        self.series_ticker_entry = tk.StringVar(value="KXBTC15M")

        self.series_ticker_entry_combo = ttk.Combobox(
        row,
        textvariable=self.series_ticker_entry,
        values=[
            "KXBTC15M",
            "KXETH15M",
            "KXSOL15M",
            "KXXRP15M",
        ],
        state="readonly",
        width=18
        )
        self.series_ticker_entry_combo.pack(side="left")

    def build_position_monitor(self):
        frame = ttk.LabelFrame(self.panel1, text='Monitor order', padding=10)
        frame.pack(fill='x', pady=10)

        row = ttk.Frame(frame); row.pack(fill='x')
        ttk.Label(row, text='Take profit %').pack(side='left')
        self.tp_entry = ttk.Entry(row, width=7)
        self.tp_entry.insert(0, '50')
        self.tp_entry.pack(side='left', padx=5)
        ttk.Label(row, text='Stop loss %').pack(side='left')
        self.sl_entry = ttk.Entry(row, width=7)
        self.sl_entry.insert(0, '50')
        self.sl_entry.pack(side='left', padx=5)

    def browse_pem(self):
        path = filedialog.askopenfilename(
            title="Select Kalshi private key PEM",
            filetypes=[("PEM files", "*.pem"), ("All files", "*.*")]
        )
        if path:
            self.private_key_path_entry.delete(0, "end")
            self.private_key_path_entry.insert(0, path)

    def build_last_order_view(self):
        frame = ttk.LabelFrame(self.panel1, text='View last order (Made in program)', padding=10)
        frame.pack(fill='x', pady=5)
        row = ttk.Frame(frame); row.pack(fill='x', pady=(6, 2))
        
        self.last_client_order_id_label = ttk.Label(row, text='Client order ID: -')
        self.last_client_order_id_label.pack(side='left')

        row = ttk.Frame(frame); row.pack(fill='x', pady=(6, 2))
        self.last_order_side_label = ttk.Label(row, text='Side: -')
        self.last_order_side_label.pack(side='left')
        row = ttk.Frame(frame); row.pack(fill='x', pady=(6, 2))
        self.last_order_price_label = ttk.Label(row, text='Contract price: -')
        self.last_order_price_label.pack(side='left')
        self.last_order_price_change = ttk.Label(row, text='Current: - ( - )')
        self.last_order_price_change.pack(side='left')
        row = ttk.Frame(frame); row.pack(fill='x', pady=(6, 2))
        self.last_order_count_label = ttk.Label(row, text='Count: -')
        self.last_order_count_label.pack(side='left')
        row = ttk.Frame(frame); row.pack(fill='x', pady=(6, 2))
        self.last_order_market_label = ttk.Label(row, text='Ticker: -')
        self.last_order_market_label.pack(side='left')

    def save_last_order(self, client_id, side, price, count, market):
        last_order = {
            "client_id": str(client_id),
            "side": str(side),
            "price": str(price),
            "count": str(count),
            "market": str(market),
        }

        with open(LAST_ORDER_FILE, "w", encoding="utf-8") as file:
            json.dump(last_order, file, indent=4)

    def load_last_order(self):
        if not LAST_ORDER_FILE.exists(): 
            return False

        try:
            with open(LAST_ORDER_FILE, "r", encoding="utf-8") as file:
                last_order_file = json.load(file)

            self.last_order_side = last_order_file.get("side", "")
            self.last_order_market_ticker = last_order_file.get("market", "")
            self.last_order_count = last_order_file.get("count", "")
            self.last_order_price = last_order_file.get("price", "")
            self.last_order_id = last_order_file.get("client_id", "")

            self.last_client_order_id_label.config(text=f"Client ID: {last_order_file.get("client_id", "")}")
            self.last_order_side_label.config(text=f"Side: {last_order_file.get("side", "")}")
            self.last_order_price_label.config(text=f"Price: {last_order_file.get("price", "")}")
            self.last_order_count_label.config(text=f"Count: {last_order_file.get("count", "")}")
            self.last_order_market_label.config(text=f"Ticker: {last_order_file.get("market", "")}")

            return True
        except (OSError, ValueError) as exc:
            return False

    def get_market_data_and_ticker(self, api, series_ticker):
        markets = api.markets(series_ticker)
        markets_data = markets['markets']
        market_data = markets_data[0]
        market_ticker = market_data['ticker'] #market_ticker, #actual_market, 
        return market_data, market_ticker

    def validate_market_for_monitoring(self, api, market_ticker):
        orders = api.get_pages('/portfolio/orders', 'orders', {'ticker': market_ticker})

        #maybe check for positions later too
        for order in orders:
            if Decimal(str(order['fill_count_fp'])) > Decimal(0.0):
                self.insert_request_text("Orders already made in market, waiting for new market.")
                raise ChangeMarketError("EXC")
                
    def get_contract_price_cents(self, market_data):
        up_price_cents = market_price_cents(market_data, 'yes') #used #in 100s
        self.insert_request_text(f"UP contract price: ${(up_price_cents / 100):.2f}.")
        return up_price_cents

    def get_side_win_estimates(self, market_data):
        estimation_method = str(self.estimation_method.get())

        if (estimation_method == 'Standard Logistical'):
            estimator = ChanceEstimator()
        elif (estimation_method == 'Monte Carlo'):
            estimator = MonteCarloEstimator()
        else:
            estimator = GARCHEstimator()

        up_win_estimate = estimator.estimate(market_data,'yes')
        #self.insert_request_text(f"Up win estimate: {(up_win_estimate * 100):.2f}%")
        
        return up_win_estimate

    def get_preferred_side_wager(self, up_win_estimate, up_price_cents, balance, multiplier):
        kelly_calculator = KellyCalculator()
        down_win_estimate = 1 - up_win_estimate
        down_price_cents = 100 - up_price_cents
        kelly_up_wager = kelly_calculator.calculate(up_win_estimate, up_price_cents, DEFAULT_FEE_CENTS, balance, multiplier)
        kelly_down_wager = kelly_calculator.calculate(down_win_estimate, down_price_cents, DEFAULT_FEE_CENTS, balance, multiplier)

        self.insert_request_text(f"Kelly up wager in dollars: ${kelly_up_wager:.2f}")
        self.insert_request_text(f"Kelly down wager in dollars: ${kelly_down_wager:.2f}")

        if (kelly_up_wager >= kelly_down_wager):
            preferred_wager = kelly_up_wager
            preferred_limit = up_price_cents 
            preferred_side = 'yes' 
        else:
            preferred_wager = kelly_down_wager 
            preferred_limit = down_price_cents 
            preferred_side = 'no' 
        side_in_text = 'up' if preferred_side == 'yes' else 'down' 
        self.insert_request_text(f"Preferred wager: ${preferred_wager:.2f}, side: {side_in_text}")
        return preferred_wager, preferred_limit, preferred_side

    def get_contract_count_limit_and_fee(self, preferred_wager, preferred_limit): 
        wager = Decimal(preferred_wager)
        limit = Decimal(preferred_limit)
        fee = Decimal(DEFAULT_FEE_CENTS)/100
        first_non_finite = next((value for value in (wager, limit, fee) if not value.is_finite()), None)
        if first_non_finite is not None:
            raise ValueError(f"Invalid value given: {first_non_finite}.")
        if not (wager > 0 and 0 < limit < 100 and fee >= 0):
            raise ValueError("Invalid value given.")
        quantized_limit = limit.quantize(Decimal("0.01"))
        if limit != quantized_limit:
            raise ValueError(f"Limit price ${limit:.2f} does not match quantized limit ${quantized_limit:.2f}.")
        limit = limit / 100
        count = int(wager // (limit + fee))

        if count < 1:
            raise ValueError("Contract count < 1.")

        if limit < 0.1:
            raise ValueError("Limit < 0.1.")

        return count, limit, fee

    def verify_balance(self, api, market_ticker, estimated_total):
        self.insert_request_text("Checking for sufficient funds in index...")
        index = api.market(market_ticker)["exchange_index"] 
        data = api.get(f"/portfolio/balance?subaccount=0&exchange_index={index}")
        breakdown = {row["exchange_index"]: Decimal(row["balance"]) for row in data.get("balance_breakdown", [])}
        balance = Decimal(data["balance_dollars"])
        required = estimated_total
        shortfall = (required - balance).quantize(Decimal("0.0001"), rounding=ROUND_UP)
        candidates = [(amount, source) for source, amount in breakdown.items() if source != index and amount >= shortfall]
        _, source = max(candidates)
        transfer_id = str(uuid4())

        if balance < estimated_total:
            self.insert_request_text("Initiating transfer...")
            api.transfer_between_indexes(source, index, shortfall, transfer_id)
            self.insert_request_text("Transferred.")
        else:
            self.insert_request_text("Sufficient funds held. Proceeding...")

    def attempt_order(self, api, market_data, preferred_side, market_ticker, count, preferred_limit):
        side_in_text = 'up' if preferred_side == 'yes' else 'down' #used
        self.insert_request_text(f"Attempting purchase of {count} {side_in_text} contracts priced at {preferred_limit} cents each.")
        order_id = str(uuid4()) 
        updated_limit = market_price_cents(market_data, preferred_side)
        updated_limit = Decimal(updated_limit)
        updated_price = updated_limit / 100
        self.insert_request_text(f"Updated price: ${updated_price:.2f}. Using original limit of ${(preferred_limit):.2f}.") 
        order_result = api.make_order(market_ticker, preferred_side, count, preferred_limit, order_id, IMMEDIATE_OR_CANCEL, False)

        if not (Decimal(str(order_result['fill_count'])) > 0.0):
            raise ValueError('Failed to buy.')
        else:     
            side_in_text = 'up' if preferred_side == 'yes' else 'down'                
            self.insert_request_text(f"Purchased {order_result['fill_count']} {side_in_text} contracts. Order ID: {order_result['client_order_id']}.")

        self.save_last_order(order_id, preferred_side, preferred_limit, order_result['fill_count'], market_ticker)
        self.load_last_order()

    def validate_market_for_positions(self, api, market_ticker):
        positions = api.get_pages('/portfolio/positions', 'market_positions', {'ticker': market_ticker})
        position_present = False
        for position in positions:
            position_present = True
            
        if not position_present:
            raise MonitorStateError('Market does not contain any unsettled positions.')

        position = positions[0]

        market_exposure = Decimal(str(position['market_exposure_dollars'])) 
        if market_exposure <= 0.0:
            raise MonitorStateError(f'{market_exposure:.2f} left.')
            
        self.insert_request_text('Valid unsettled positions found. Proceeding...')
        self.insert_request_text(f'Monitoring {self.last_order_id}')

    def attempt_sell(self, api, tp, sl, side_to_sell, market_ticker):
        count = Decimal(str(self.last_order_count))
        original_price = Decimal(str(self.last_order_price))

        bid, liquidity = api.executable_bid(market_ticker, side_to_sell)

        target = original_price * (1 + tp)
        stop = original_price * (1 - sl)

       # markets = api.markets(series_ticker)
        #markets_data = markets['markets']
        market_data = api.market(market_ticker)

        minutes_remaining = self.get_minutes_remaining(market_data)
        self.insert_request_text(f'Minutes remaining: {minutes_remaining:.2f}')
        up_side_win_estimate = self.get_side_win_estimates(market_data)

        if bid is None:
            raise ValueError("Monitoring: no executable bid.")

        reason = 'TAKE PROFIT' if bid >= target else 'STOP LOSS'
        percent_change = ((bid/original_price) - 1)
        percent_change = Decimal(percent_change)
        self.insert_request_text(f'Current price: ${bid:.2f}; {percent_change * 100:.2f}%;')
        self.last_order_price_change.config(text=f'${bid:.2f} ({percent_change * 100:.2f}%)')
        #forecast_time = 0.083

        #if (minutes_remaining > forecast_time):
        #    forecast_estimator = ForecastEstimator()
        #    forecast = forecast_estimator.estimate(market_data, side_to_sell, forecast_time)
        #    forecast = Decimal(forecast)
        #    self.insert_request_text(f'{forecast_time} min forecast: {(((forecast/original_price)-1) * 100):.2f}%.')

        # forecast 
        last_percent_change = self.last_percent_change
        self.last_percent_change = percent_change

        if stop < bid < target:
            raise ValueError(f'Holding.')
        
            #if (bid > original_price):
            #    if (minutes_remaining > 5) and (percent_change >= 0.3):
            #        reason = 'TAKE PROFIT'
           #         self.insert_request_text(f'Bypassing target and stop values as (minutes_remaining > 5) and ((bid - original_price) >= 0.3).')
            #    else:
           #         raise ValueError(f'Holding.')
          #  elif (bid < original_price):
          #      if (minutes_remaining < 5) and (bid < 0.5):
          #          self.insert_request_text(f'Bypassing target and stop values as (minutes_remaining < 3) and (bid < 0.5).')
           #     else:
          #          raise ValueError(f'Holding.')
         #   elif (bid == original_price):
         #       raise ValueError(f'Holding.')

       # if bid <= stop:
       #     hold_min = 10
       #     if (minutes_remaining > hold_min):
      #          self.insert_request_text(f'Bypassing target and stop values as (minutes_remaining > {hold_min}).')
      #          raise ValueError(f'Holding.')
       #     else:
      #          if last_percent_change is not None:
      #              diff = percent_change - last_percent_change 
       #             self.last_percent_change = None
      #              if (diff < -0.2):
      #                  raise ValueError(f'Holding due to volatility.')

      #  if bid >= target:
      #      if (minutes_remaining < 5):
      #          self.insert_request_text(f'Bypassing target and stop values as (minutes_remaining < 5).')
       #         raise ValueError(f'Holding.')

        self.insert_request_text(f'Attempting to sell to {reason}.')

        sellable_qty = min(count, liquidity).quantize(Decimal('0.01'), rounding='ROUND_DOWN')
        if sellable_qty <= 0:
            raise ValueError(f'Liquidity conflict in bid. Continuing. Count is {count} and liquidity is {liquidity}.')
        
        sell_order_id = str(uuid4())
        exit_side = "no" if side_to_sell == "yes" else "yes"
        exit_price = Decimal("1") - bid

        result = api.make_order(
            market_ticker,
            exit_side,
            sellable_qty,
            exit_price,
            sell_order_id,
            IMMEDIATE_OR_CANCEL,
            True
        )
        fill_count = Decimal(str(result['fill_count']))

        if not (fill_count > 0.0):
            raise ValueError(f'Failed to sell, restarting loop.')
        else:
            new_count = count - fill_count
            self.insert_request_text(f'Sold {fill_count} contracts. {new_count} left.')
            self.save_last_order(self.last_order_id, self.last_order_side, self.last_order_price, new_count, self.last_order_market_ticker)
            self.load_last_order()

            if (new_count <= 0.0):
                raise MonitorStateError("All contracts sold or unavailable.")
            else:
                raise ValueError("Unsettled contracts still available in position.")

    def get_minutes_remaining(self, market_data):
        close_time = datetime.fromisoformat(
            market_data["close_time"].replace("Z", "+00:00")
        )

        current_time = datetime.now(timezone.utc)

        return max(0, (close_time - current_time).total_seconds() / 60)

    def build_other_settings(self):
        frame = ttk.LabelFrame(self.panel1, text='Other settings', padding=10)
        frame.pack(fill='x', pady=5)
        row = ttk.Frame(frame); row.pack(fill='x', pady=(6, 2))

        self.enable_market_switching = tk.BooleanVar(value=False)
        self.enable_market_switching_button = tk.Checkbutton(row, text='Enable market switching',variable=self.enable_market_switching, onvalue=1, offvalue=0, command=None)
        self.enable_market_switching_button.pack(side='left')

        row = ttk.Frame(frame); row.pack(fill='x', pady=(6, 2))
        ttk.Label(row, text='Estimation method: ', width = 15).pack(side='left')
        
        self.estimation_method = tk.StringVar(value="Standard Logistical")

        self.estimation_method_combo = ttk.Combobox(
        row,
        textvariable=self.estimation_method,
        values=[
            "Standard Logistical", "Monte Carlo", "Garche"
        ],
        state="readonly",
        width=18
        )
        self.estimation_method_combo.pack(side="left")

    def print_cat(self):
        #self.print_horse()
        lines = [
"           ⡀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢠⡀⠀⠀⠀⠀⠀⠀⠀⠀",
"⠀⠀⠀⠀⠀⠀⠀⠀⣼⣿⣆⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣴⢿⣧⠀⠀⠀⠀⠀⠀⠀⠀",
"⠀⠀⠀⠀⠀⠀⠀⢠⣿⠉⢻⣦⠀⠀⠀⠀⠀⠀⠀⠀⢀⣴⠏⠈⣿⣆⠀⠀⠀⠀⠀⠀⠀",
"⠀⠀⠀⠀⠀⠀⢀⣿⠃⠀⠀⠹⣷⡄⠀⠀⠀⠀⠀⣠⡾⠇⠀⠀⠘⣿⡆⠀⠀⠀⠀⠀⠀",
"⠀⠀⠀⠀⠀ ⣾⠃⠀⠀⠄⠈⠌⢿⣦⣤⣤⣤⣴⠟⠡⠀⠀⠠⠀⠘⣿⡄⠀⠀⠀⠀⠀",
"⠀⠀⠀⠀⠀⣸⡟⠀⠀⢂⠄⠊⠀⠀⠉⠁⠀⠈⠉⠀⠀⠐⠠⡀⠀⠀ ⠹⣷⡀⠀⠀⠀⠀",
"⠀⠀⠀⠀⢰⡿⠀⠀⠈⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠑⠀   ⠀⢻⣧⠀⠀⠀⠀",
"⠀⠀⠀⢀⡿⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀   ⢻⡆⠀⠀⠀",
"⠀⢀⣀⣼⣷⣤⣤⣤⣤⣤⣤⣤⣤⣄⣀⣀⣀⠀⢀⣀⣀⣠⣤⣤⣤⣤⣤⣤⣼⣧⡀⠀⠀",
"⣴⣿⠟⠉⠁⢠⣾⣿⣯⠙⠛⠛⠉⠙⠻⣿⡿⠿⢿⣿⣿⠟⠛⠉⣿⣿⣿⡟⠛⠛⢿⣿⡄",
"⣿⣿⡄⠀⠀⠸⣿⣿⡿⠁⠀⠀⠀⠀⣠⣿⠁⠀⠀⠹⣿⣄⠀⠀⢿⣿⣿⠟⠀⠀⠀  ⣿⠇",
"⠈⠻⣿⣷⣶⣤⣤⣤⣤⣤⣤⣤⣴⡾⠿⠃⠀⠀⠀⠀⠈⠻⠿⢶⣶⣾⣧⣤⣤⡶⣿⠛⠀",
"⠀⠀⣿⠀⠉⠉⠉⠉⠉⠉⠀⠀⠀⠀⠀⠀⠀⠀⠀⠠⠦⡄⠀⠀⠀⠀⠀⠀⠈⠀  ⢻⡆⠀",
"⠀⠀⣿⠀⠁⠀⠀⠀⠀⠂⠀⠀⣾⣁⣤⢷⣄⣀⣀⣀⡴⠃⠀⠀⠀⠀⠀⠀⠈⠀  ⢸⡇⠀",
"⠀⠀⢿⡄⠀⠀⠀⠀⠀⠀⠀⠀⠉⠉⠉⠈⠉⠛⠛⠋⠁⠀⠀⠀⠀⠀⠀⠀⠀  ⢠⣾⠃⠀",
"⠀⠀⠈⠛⠷⣶⣤⣀⣀⣀⣀⡀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣀⣀⣀⣀⣠⣤⣶⠶⠟⠁⠀⠀",
"⠀⠀⠀⠀⠀⠀⠈⠉⠉⠉⠙⠛⠛⠛⠛⠛⠛⠛⠛⠛⠛⠛⠋⠉⠉⠉⠁⠀⠀⠀⠀",

        ]

        for line in reversed(lines):
            self.insert_request_text(f'{line}')

  #  def print_horse(self):
        # request_text is your existing tk.Text widget
   #     self.app_text.image = tk.PhotoImage(file= project_root / "images/horse.jpg")
   #     self.app_text.image_create(1.0, image=self.app_text.image)
    #    self.app_text.insert(1.0, "\n")
   #     self.app_text.see(1.0)

    def print_horse(self):
        text = self.app_text

        with Image.open(project_root / "images" / "horse.jpg") as horse:
            text.image = ImageTk.PhotoImage(horse, master=text)

        # A read-only log needs to be writable while inserting the image.
        original_state = text.cget("state")
        #text.configure(state="normal")

        text.image_create("1.0", image=text.image)
        text.insert("1.1", "\n")
        text.see("1.0")

          #  text.configure(state=original_state)

class MonitorStateError(Exception):
    """Raised when state 2 call results in an error indicating wrong state choice."""
    pass

class ChangeMarketError(Exception):
    pass

if __name__ == '__main__':
    root = tk.Tk()
    app = LoopApp(root)
    root.mainloop()
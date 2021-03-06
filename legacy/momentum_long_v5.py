import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Tuple

import alpaca_trade_api as tradeapi
import numpy as np
from liualgotrader.common import config
from liualgotrader.common.tlog import tlog
from liualgotrader.common.trading_data import (buy_indicators, buy_time,
                                               cool_down, last_used_strategy,
                                               latest_cost_basis,
                                               latest_scalp_basis, open_orders,
                                               sell_indicators, stop_prices,
                                               target_prices)
from liualgotrader.fincalcs.support_resistance import find_stop
from liualgotrader.strategies.base import Strategy, StrategyType
from pandas import DataFrame as df
from talib import BBANDS, MACD, RSI


class MomentumLongV5(Strategy):
    name = "momentum_long_v5"
    whipsawed: Dict = {}
    down_cross: Dict = {}

    def __init__(
        self,
        batch_id: str,
        schedule: List[Dict],
        ref_run_id: int = None,
        check_patterns: bool = False,
    ):
        self.check_patterns = check_patterns
        super().__init__(
            name=self.name,
            type=StrategyType.DAY_TRADE,
            batch_id=batch_id,
            ref_run_id=ref_run_id,
            schedule=schedule,
        )

    async def buy_callback(self, symbol: str, price: float, qty: int) -> None:
        latest_scalp_basis[symbol] = latest_cost_basis[symbol] = price

    async def sell_callback(self, symbol: str, price: float, qty: int) -> None:
        latest_scalp_basis[symbol] = price

    async def create(self) -> None:
        await super().create()
        tlog(f"strategy {self.name} created")

    async def should_cool_down(self, symbol: str, now: datetime):
        if (
            symbol in cool_down
            and cool_down[symbol]
            and cool_down[symbol] >= now.replace(second=0, microsecond=0)  # type: ignore
        ):
            return True

        cool_down[symbol] = None
        return False

    async def run(
        self,
        symbol: str,
        shortable: bool,
        position: int,
        minute_history: df,
        now: datetime,
        portfolio_value: float = None,
        trading_api: tradeapi = None,
        debug: bool = False,
        backtesting: bool = False,
    ) -> Tuple[bool, Dict]:
        data = minute_history.iloc[-1]
        prev_min = minute_history.iloc[-2]

        morning_rush = (
            True if (now - config.market_open).seconds // 60 < 30 else False
        )

        if (
            await super().is_buy_time(now)
            and not position
            and not open_orders.get(symbol, None)
            and not await self.should_cool_down(symbol, now)
            and data.volume > 500
        ):
            close = (
                minute_history["close"].dropna().between_time("9:30", "16:00")
            )

            # calc macd on 5 min
            close_5min = (
                minute_history["close"]
                .dropna()
                .between_time("9:30", "16:00")
                .resample("5min")
                .last()
            ).dropna()

            macds = MACD(close_5min, 13, 21)
            macd = macds[0]
            macd_signal = macds[1]

            # check if zero-crossing into negative, mark that point
            if macd[-1] < 0 <= macd[-2] and not self.down_cross.get(
                symbol, None
            ):
                self.down_cross[symbol] = data.close
                tlog(
                    f"{self.name}: [{now}]{symbol} identified down-ward zero-crossing of 5-min MACD w/ { self.down_cross[symbol]}"
                )
                return False, {}
            elif self.down_cross.get(symbol, None) and macd[-1] >= 0:
                self.down_cross[symbol] = None
                tlog(
                    f"{self.name}: [{now}]identified up-ward zero-crossing of 5-min MACD"
                )
                return False, {}

            to_buy = False
            reason = []
            # if passed zero crossing -> look for change in trend
            if (
                self.down_cross.get(symbol, None)
                and macd[-1] > macd[-2] > macd[-3]
                and macd[-1] > macd_signal[-1] > macd_signal[-2]
                and macd[-2] > macd_signal[-2]
            ):
                tlog(
                    f"{self.name}: [{now}]{symbol }identified up-ward trend {macd[-1]}, {macd[-2]}, {macd[-3]} price {data.close}"
                )

                # check if price actually went down and not up
                if data.close < self.down_cross[symbol]:
                    to_buy = True
                    reason.append("MACD signal")

            if to_buy:
                # check RSI does not indicate overbought
                rsi = RSI(close, 14)

                if debug:
                    tlog(
                        f"[{self.name}][{now}] {symbol} RSI={round(rsi[-1], 2)}"
                    )

                rsi_limit = 75
                if rsi[-1] < rsi_limit:
                    if debug:
                        tlog(
                            f"[{self.name}][{now}] {symbol} RSI {round(rsi[-1], 2)} <= {rsi_limit}"
                        )
                else:
                    tlog(
                        f"[{self.name}][{now}] {symbol} RSI over-bought, cool down for 5 min"
                    )
                    cool_down[symbol] = now.replace(
                        second=0, microsecond=0
                    ) + timedelta(minutes=5)

                    return False, {}

                stop_price = data.close * 0.96
                target_price = self.down_cross[symbol] * 1.12
                target_prices[symbol] = target_price
                stop_prices[symbol] = stop_price

                if portfolio_value is None:
                    if trading_api:

                        retry = 3
                        while retry > 0:
                            try:
                                portfolio_value = float(
                                    trading_api.get_account().portfolio_value
                                )
                                break
                            except ConnectionError as e:
                                tlog(
                                    f"[{symbol}][{now}[Error] get_account() failed w/ {e}, retrying {retry} more times"
                                )
                                await asyncio.sleep(0)
                                retry -= 1

                        if not portfolio_value:
                            tlog(
                                "f[{symbol}][{now}[Error] failed to get portfolio_value"
                            )
                            return False, {}
                    else:
                        raise Exception(
                            f"{self.name}: both portfolio_value and trading_api can't be None"
                        )

                shares_to_buy = (
                    portfolio_value
                    * config.risk
                    // (data.close - stop_prices[symbol])
                )
                if not shares_to_buy:
                    shares_to_buy = 1
                shares_to_buy -= position
                if shares_to_buy > 0:
                    self.whipsawed[symbol] = False

                    buy_price = max(data.close, data.vwap)
                    tlog(
                        f"[{self.name}][{now}] Submitting buy for {shares_to_buy} shares of {symbol} at {buy_price} target {target_prices[symbol]} stop {stop_prices[symbol]}"
                    )

                    buy_indicators[symbol] = {
                        "macd": macd[-5:].tolist(),
                        "macd_signal": macd_signal[-5:].tolist(),
                        "vwap": data.vwap,
                        "avg": data.average,
                        "reason": reason,
                    }

                    return (
                        True,
                        {
                            "side": "buy",
                            "qty": str(shares_to_buy),
                            "type": "limit",
                            "limit_price": str(buy_price),
                        }
                        if not morning_rush
                        else {
                            "side": "buy",
                            "qty": str(shares_to_buy),
                            "type": "market",
                        },
                    )
        elif (
            await super().is_sell_time(now)
            and position > 0
            and symbol in latest_cost_basis
            and last_used_strategy[symbol].name == self.name
            and not open_orders.get(symbol)
        ):
            if (
                not self.whipsawed.get(symbol, None)
                and data.close < latest_cost_basis[symbol] * 0.99
            ):
                self.whipsawed[symbol] = True

            serie = (
                minute_history["close"].dropna().between_time("9:30", "16:00")
            )

            if data.vwap:
                serie[-1] = data.vwap

            macds = MACD(
                serie,
                13,
                21,
            )

            macd = macds[0]
            macd_signal = macds[1]
            rsi = RSI(
                minute_history["close"].dropna().between_time("9:30", "16:00"),
                14,
            )

            movement = (
                data.close - latest_scalp_basis[symbol]
            ) / latest_scalp_basis[symbol]
            max_movement = (
                minute_history["close"][buy_time[symbol] :].max()
                - latest_scalp_basis[symbol]
            ) / latest_scalp_basis[symbol]
            macd_val = macd[-1]
            macd_signal_val = macd_signal[-1]

            round_factor = (
                2 if macd_val >= 0.1 or macd_signal_val >= 0.1 else 3
            )
            scalp_threshold = (
                target_prices[symbol] + latest_scalp_basis[symbol]
            ) / 2.0

            macd_below_signal = round(macd_val, round_factor) < round(
                macd_signal_val, round_factor
            )

            bail_out = (
                (
                    latest_scalp_basis[symbol] > latest_cost_basis[symbol]
                    or (max_movement > 0.02 and max_movement > movement)
                )
                and macd_below_signal
                and round(macd[-1], round_factor)
                < round(macd[-2], round_factor)
            )
            bail_on_whipsawed = (
                self.whipsawed.get(symbol, False)
                and movement > 0.01
                and macd_below_signal
                and round(macd[-1], round_factor)
                < round(macd[-2], round_factor)
            )
            scalp = movement > 0.04 or data.vwap > scalp_threshold
            below_cost_base = data.vwap < latest_cost_basis[symbol]

            rsi_limit = 79 if not morning_rush else 85
            to_sell = False
            partial_sell = False
            limit_sell = False
            sell_reasons = []
            if data.close <= stop_prices[symbol]:
                to_sell = True
                sell_reasons.append("stopped")
            elif data.close >= target_prices[symbol] and macd[-1] <= 0:
                to_sell = True
                sell_reasons.append("above target & macd negative")
            elif rsi[-1] >= rsi_limit:
                to_sell = True
                sell_reasons.append("rsi max, cool-down for 5 minutes")
                cool_down[symbol] = now.replace(
                    second=0, microsecond=0
                ) + timedelta(minutes=5)
            elif bail_out:
                to_sell = True
                sell_reasons.append("bail")
            elif scalp:
                partial_sell = True
                to_sell = True
                sell_reasons.append("scale-out")
            elif bail_on_whipsawed:
                to_sell = True
                partial_sell = False
                limit_sell = True
                sell_reasons.append("bail post whipsawed")
            # elif macd[-1] < macd_signal[-1] <= macd_signal[-2] < macd[-2]:
            #    to_sell = True
            #    sell_reasons.append("MACD cross signal from above")

            if to_sell:
                sell_indicators[symbol] = {
                    "rsi": rsi[-3:].tolist(),
                    "movement": movement,
                    "sell_macd": macd[-5:].tolist(),
                    "sell_macd_signal": macd_signal[-5:].tolist(),
                    "vwap": data.vwap,
                    "avg": data.average,
                    "reasons": " AND ".join(
                        [str(elem) for elem in sell_reasons]
                    ),
                }

                if not partial_sell:
                    if not limit_sell:
                        tlog(
                            f"[{self.name}][{now}] Submitting sell for {position} shares of {symbol} at market with reason:{sell_reasons}"
                        )
                        return (
                            True,
                            {
                                "side": "sell",
                                "qty": str(position),
                                "type": "market",
                            },
                        )
                    else:
                        tlog(
                            f"[{self.name}][{now}] Submitting sell for {position} shares of {symbol} at {data.close} with reason:{sell_reasons}"
                        )
                        return (
                            True,
                            {
                                "side": "sell",
                                "qty": str(position),
                                "type": "limit",
                                "limit_price": str(data.close),
                            },
                        )
                else:
                    qty = int(position / 2) if position > 1 else 1
                    tlog(
                        f"[{self.name}][{now}] Submitting sell for {str(qty)} shares of {symbol} at limit of {data.close }with reason:{sell_reasons}"
                    )
                    return (
                        True,
                        {
                            "side": "sell",
                            "qty": str(qty),
                            "type": "limit",
                            "limit_price": str(data.close),
                        },
                    )

        return False, {}

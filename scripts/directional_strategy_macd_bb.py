from decimal import Decimal

from hummingbot.data_feed.candles_feed.candles_factory import CandlesFactory
from hummingbot.strategy.directional_strategy_base import DirectionalStrategyBase


class MacdBB(DirectionalStrategyBase):
    directional_strategy_name: str = "MACD_BB"
    # Define the trading pair and exchange that we want to use and the csv where we are going to store the entries
    trading_pair: str = "FOOTBALL-USDT"
    exchange: str = "binance_perpetual"
    order_amount_usd = Decimal("15")
    leverage = 10

    # Configure the parameters for the position
    stop_loss: float = 0.0075
    take_profit: float = 0.015
    time_limit: int = 60 * 55
    trailing_stop_activation_delta = 0.003
    trailing_stop_trailing_delta = 0.0007

    candles = [CandlesFactory.get_candle(connector=exchange,
                                         trading_pair=trading_pair,
                                         interval="3m", max_records=150)]
    markets = {exchange: {trading_pair}}

    def get_signal(self):
        candles_df = self.get_processed_df()
        last_candle = candles_df.iloc[-1]
        bbp = last_candle["BBP_100_2.0"]
        macdh = last_candle["MACDh_21_42_9"]
        macd = last_candle["MACD_21_42_9"]
        if bbp < 0.4 and macdh > 0 and macd < 0:
            signal_value = 1
        elif bbp > 0.6 and macdh < 0 and macd > 0:
            signal_value = -1
        else:
            signal_value = 0
        return signal_value

    def get_processed_df(self):
        candles_df = self.candles[0].candles_df
        candles_df.ta.bbands(length=100, append=True)
        candles_df.ta.macd(fast=21, slow=42, signal=9, append=True)
        return candles_df

    def market_data_extra_info(self):
        lines = []
        columns_to_show = ["timestamp", "open", "low", "high", "close", "volume", "BBP_100_2.0", "MACDh_21_42_9", "MACD_21_42_9"]
        candles_df = self.get_processed_df()
        lines.extend([f"Candles: {self.candles[0].name} | Interval: {self.candles[0].interval}\n"])
        lines.extend(self.candles_formatted_list(candles_df, columns_to_show))
        return lines
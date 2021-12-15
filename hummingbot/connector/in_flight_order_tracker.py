import logging
import asyncio
import time

from decimal import Decimal
from typing import Dict, Optional
from cachetools import TTLCache

from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState, OrderUpdate, TradeUpdate
from hummingbot.core.event.events import (
    BuyOrderCompletedEvent,
    BuyOrderCreatedEvent,
    MarketEvent,
    MarketOrderFailureEvent,
    OrderCancelledEvent,
    OrderFilledEvent,
    SellOrderCompletedEvent,
    SellOrderCreatedEvent,
    TradeType,
)
from hummingbot.core.event.event_logger import EventLogger
from hummingbot.core.event.event_reporter import EventReporter
from hummingbot.core.pubsub import PubSub
from hummingbot.logger.logger import HummingbotLogger

ifot_logger = None


class InFlightOrderTracker(PubSub):

    MAX_CACHE_SIZE = 1000
    CACHED_ORDER_TTL = 30.0  # seconds
    MARKET_EVENTS = [
        MarketEvent.BuyOrderCompleted,
        MarketEvent.SellOrderCompleted,
        MarketEvent.OrderCancelled,
        MarketEvent.OrderFilled,
        MarketEvent.OrderFailure,
        MarketEvent.BuyOrderCreated,
        MarketEvent.SellOrderCreated,
        # MarketEvent.FundingPaymentCompleted,
        # MarketEvent.RangePositionCreated,
        # MarketEvent.RangePositionRemoved,
        # MarketEvent.RangePositionUpdated,
        # MarketEvent.RangePositionFailure,
        # MarketEvent.RangePositionInitiated,
    ]

    @classmethod
    def logger(cls) -> HummingbotLogger:
        global ifot_logger
        if ifot_logger is None:
            ifot_logger = logging.getLogger(__name__)
        return ifot_logger

    def __init__(self) -> None:
        """
        Provides utilities for connectors to update in-flight orders and also handle order errors.
        Also it maintains cached orders to allow for additional updates to occur after the original order is determined to
        no longer be active.
        An error constitutes, but is not limited to, the following:
        (1) Order not found on exchange.
        (2) Cannot retrieve exchange_order_id of an order
        (3) Error thrown by exchange when fetching order status
        """

        self._in_flight_orders: Dict[str, InFlightOrder] = {}
        self._cached_orders: TTLCache = TTLCache(maxsize=self.MAX_CACHE_SIZE, ttl=self.CACHED_ORDER_TTL)

        self._order_tracking_task: Optional[asyncio.Task] = None
        self._last_poll_timestamp: int = -1

        self._event_reporter = EventReporter(event_source=__name__)
        self._event_logger = EventLogger(event_source=__name__)
        for event_tag in self.MARKET_EVENTS:
            self.add_listener(event_tag, self._event_reporter)
            self.add_listener(event_tag, self._event_logger)

    @property
    def active_orders(self) -> Dict[str, InFlightOrder]:
        """
        Returns orders that are actively tracked
        """
        return self._in_flight_orders

    @property
    def cached_orders(self) -> Dict[str, InFlightOrder]:
        """
        Returns orders that are no longer actively tracked.
        """
        return self._cached_orders

    @property
    def current_timestamp(self) -> int:
        """
        Returns current timestamp in milliseconds.
        """
        return int(time.time() * 1e3)

    def start_tracking_order(self, order: InFlightOrder):
        self._in_flight_orders[order.client_order_id] = order

    def stop_tracking_order(self, client_order_id: str):
        if client_order_id in self._in_flight_orders:
            self._cached_orders[client_order_id] = self._in_flight_orders[client_order_id]
            del self._in_flight_orders[client_order_id]

    def fetch_tracked_order(self, client_order_id: str) -> Optional[InFlightOrder]:
        return self._in_flight_orders.get(client_order_id, None)

    def fetch_cached_order(self, client_order_id: str) -> Optional[InFlightOrder]:
        return self._cached_orders.get(client_order_id, None)

    def fetch_order(self, client_order_id: str) -> Optional[InFlightOrder]:
        return self._in_flight_orders.get(client_order_id, self._cached_orders.get(client_order_id, None))

    def _trigger_created_event(self, order: InFlightOrder):
        event_tag = MarketEvent.BuyOrderCreated if order.trade_type is TradeType.BUY else MarketEvent.SellOrderCreated
        event_class = BuyOrderCreatedEvent if order.trade_type is TradeType.BUY else SellOrderCreatedEvent
        self.trigger_event(
            event_tag,
            event_class(
                self.current_timestamp,
                order.order_type,
                order.trading_pair,
                order.amount,
                order.price,
                order.client_order_id,
                exchange_order_id=order.exchange_order_id,
            ),
        )

    def _trigger_cancelled_event(self, order: InFlightOrder):
        self.trigger_event(
            MarketEvent.OrderCancelled,
            OrderCancelledEvent(
                timestamp=self.current_timestamp,
                order_id=order.client_order_id,
            ),
        )

    def _trigger_completed_event(self, order: InFlightOrder):
        event_tag = (
            MarketEvent.BuyOrderCompleted if order.trade_type is TradeType.BUY else MarketEvent.SellOrderCompleted
        )
        event_class = BuyOrderCompletedEvent if order.trade_type is TradeType.BUY else SellOrderCompletedEvent
        self.trigger_event(
            event_tag,
            event_class(
                self.current_timestamp,
                order.client_order_id,
                order.base_asset,
                order.quote_asset,
                order.fee_asset,
                order.executed_amount_base,
                order.executed_amount_quote,
                order.cumulative_fee_paid,
                order.order_type,
                order.exchange_order_id,
            ),
        )

    def _trigger_failure_event(self, order: InFlightOrder):
        self.trigger_event(
            MarketEvent.OrderFailure,
            MarketOrderFailureEvent(
                timestamp=self.current_timestamp,
                order_id=order.client_order_id,
                order_type=order.order_type,
            ),
        )

    def _trigger_order_creation(self, tracked_order: InFlightOrder, previous_state: OrderState, new_state: OrderState):
        if previous_state == OrderState.PENDING_CREATE and new_state == OrderState.OPEN:
            self.logger().info(
                f"Created {tracked_order.order_type.name.upper()} {tracked_order.trade_type.name.upper()} order "
                f"{tracked_order.client_order_id} for {tracked_order.amount} {tracked_order.trading_pair}."
            )
            self._trigger_created_event(tracked_order)

    def _trigger_order_fills(self, tracked_order: InFlightOrder, prev_executed_amount_base: Decimal):
        if prev_executed_amount_base < tracked_order.executed_amount_base:
            self.logger().info(
                f"The {tracked_order.trade_type.name.upper()} order {tracked_order.client_order_id} "
                f"amounting to {tracked_order.executed_amount_base}/{tracked_order.amount} "
                f"{tracked_order.base_asset} has been filled."
            )
            self.trigger_event(
                MarketEvent.OrderFilled,
                OrderFilledEvent(
                    self.current_timestamp,
                    tracked_order.client_order_id,
                    tracked_order.trading_pair,
                    tracked_order.trade_type,
                    tracked_order.order_type,
                    tracked_order.last_filled_price,
                    tracked_order.last_filled_amount,
                    tracked_order.latest_trade_fee,
                    tracked_order.exchange_order_id,
                ),
            )

    def _trigger_order_completion(self, tracked_order: InFlightOrder, order_update: Optional[OrderUpdate] = None):
        if tracked_order.is_open:
            return

        if tracked_order.is_cancelled:
            self._trigger_cancelled_event(tracked_order)
            self.logger().info(f"Successfully cancelled order {tracked_order.client_order_id}.")

        elif tracked_order.is_filled:
            self._trigger_completed_event(tracked_order)
            self.logger().info(
                f"{tracked_order.trade_type.name.upper()} order {tracked_order.client_order_id} completely filled. "
            )

        elif tracked_order.is_failure:
            self._trigger_failure_event(tracked_order)
            self.logger().info(f"Order {tracked_order.client_order_id} has failed. Order Update: {order_update}")

        self.stop_tracking_order(tracked_order.client_order_id)

    def process_order_update(self, order_update: OrderUpdate):
        client_order_id: str = order_update.client_order_id

        tracked_order: Optional[InFlightOrder] = self.fetch_order(client_order_id)

        if tracked_order:
            previous_state: OrderState = tracked_order.current_state
            previous_executed_amount_base: Decimal = tracked_order.executed_amount_base

            updated: bool = tracked_order.update_with_order_update(order_update)
            if updated:
                self._trigger_order_creation(tracked_order, previous_state, order_update.new_state)
                self._trigger_order_fills(tracked_order, previous_executed_amount_base)
                self._trigger_order_completion(tracked_order, order_update)

        else:
            self.logger().error(f"Order {client_order_id} no longer being tracked. {order_update}", exc_info=True)

    def process_trade_update(self, trade_update: TradeUpdate):
        client_order_id: str = trade_update.client_order_id

        tracked_order: Optional[InFlightOrder] = self.fetch_order(client_order_id)

        if tracked_order:
            previous_executed_amount_base: Decimal = tracked_order.executed_amount_base

            updated: bool = tracked_order.update_with_trade_update(trade_update)
            if updated:
                self._trigger_order_fills(tracked_order, previous_executed_amount_base)
                self._trigger_order_completion(tracked_order, trade_update)
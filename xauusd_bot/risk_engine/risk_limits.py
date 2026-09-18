"""
Account-level risk limits (architecture doc §5.3-5.6).

Daily-loss and drawdown checks are pure functions of AccountState — no
hidden state, trivially testable. Consecutive-loss protection needs a
cooldown WINDOW, not just a count, so it's a small stateful tracker
instead — but its state is explicit and injected, never hidden as a
module-level global.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from config.config_schema import RiskConfig
from core.enums import RejectReason
from core.models import AccountState


def check_max_open_positions(account: AccountState, risk: RiskConfig) -> Optional[RejectReason]:
    if account.open_positions_count >= risk.max_open_positions:
        return RejectReason.POSITION_ALREADY_OPEN
    return None


def check_daily_loss_limit(account: AccountState, risk: RiskConfig) -> Optional[RejectReason]:
    if account.daily_loss_percent >= risk.daily_loss_limit_percent:
        return RejectReason.DAILY_LOSS_LIMIT_HIT
    return None


def check_max_drawdown(account: AccountState, risk: RiskConfig) -> Optional[RejectReason]:
    if account.drawdown_percent >= risk.max_drawdown_percent:
        return RejectReason.MAX_DRAWDOWN_HIT
    return None


@dataclass
class ConsecutiveLossTracker:
    """Tracks consecutive losses and enforces a cooldown window once the
    configured limit is hit. A winning trade resets the streak to zero —
    this is explicitly NOT martingale/averaging logic; it only ever
    controls whether new trades are ALLOWED, never position size."""

    max_consecutive_losses: int
    cooldown_hours: int
    consecutive_losses: int = 0
    cooldown_until: Optional[datetime] = field(default=None)

    def record_result(self, *, is_win: bool, at: Optional[datetime] = None) -> None:
        at = at or datetime.now(timezone.utc)
        if is_win:
            self.consecutive_losses = 0
            self.cooldown_until = None
            return

        self.consecutive_losses += 1
        if self.consecutive_losses >= self.max_consecutive_losses:
            self.cooldown_until = at + timedelta(hours=self.cooldown_hours)

    def is_in_cooldown(self, *, now: Optional[datetime] = None) -> bool:
        if self.cooldown_until is None:
            return False
        now = now or datetime.now(timezone.utc)
        return now < self.cooldown_until

    def reset(self) -> None:
        """Manual reset — e.g. operator override after reviewing losses.
        Never called automatically by the bot itself."""
        self.consecutive_losses = 0
        self.cooldown_until = None

    @classmethod
    def from_config(cls, risk: RiskConfig) -> "ConsecutiveLossTracker":
        return cls(
            max_consecutive_losses=risk.max_consecutive_losses,
            cooldown_hours=risk.consecutive_loss_cooldown_hours,
        )


def check_consecutive_loss_cooldown(
    tracker: ConsecutiveLossTracker, *, now: Optional[datetime] = None
) -> Optional[RejectReason]:
    if tracker.is_in_cooldown(now=now):
        return RejectReason.CONSECUTIVE_LOSS_COOLDOWN_ACTIVE
    return None

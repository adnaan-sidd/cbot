"""
Kill switch (architecture doc §5 Safety Controls, §6 state machine —
EMERGENCY_HALT/KILL_SWITCH_ACTIVE reachable from any state).

Automatic only, per the decision log — no manual CLI/Telegram trigger
in this version (`KillSwitchConfig.manual_trigger_enabled` stays False;
see config_schema.py). Two triggers: max-drawdown breach (using the
REAL, mark-to-market equity from AccountTracker — this is the capability
Phase 3's backtest engine explicitly lacked) and broker disconnect.

Once triggered, the switch stays tripped (`armed=False`) until
`rearm()` is called — and `rearm()` is never called automatically
anywhere in this codebase. It exists for an operator to call after
reviewing what happened, consistent with `manual_rearm_required=True`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from config.config_schema import KillSwitchConfig, RiskConfig
from core.models import AccountState


@dataclass
class KillSwitch:
    risk: RiskConfig
    config: KillSwitchConfig
    armed: bool = field(default=True)
    triggered_reason: Optional[str] = field(default=None)

    def check(self, account: AccountState, *, broker_connected: bool) -> bool:
        """Returns True if trading should be halted RIGHT NOW — either
        because this call just tripped the switch, or because it was
        already tripped and remains so. Callers should treat any True
        return as "no new trades, and if a position is open, close it,"
        regardless of which case produced it."""
        if not self.armed:
            return True

        if self.config.trigger_on_max_drawdown and account.drawdown_percent >= self.risk.max_drawdown_percent:
            self._trigger(
                f"max drawdown breached: {account.drawdown_percent:.2%} >= "
                f"{self.risk.max_drawdown_percent:.2%} (equity={account.equity:.2f}, peak={account.peak_equity:.2f})"
            )
            return True

        if self.config.trigger_on_broker_disconnect and not broker_connected:
            self._trigger("broker connection lost")
            return True

        return False

    def _trigger(self, reason: str) -> None:
        self.armed = False
        self.triggered_reason = reason

    def rearm(self) -> None:
        """Manual operator action — never called automatically. Clears
        the triggered state so trading can resume. Does not undo
        whatever position-closing already happened as a result of the
        trigger; it only re-permits new trading going forward."""
        self.armed = True
        self.triggered_reason = None

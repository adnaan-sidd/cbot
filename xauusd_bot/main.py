"""
Phase 0 entry point.

This intentionally does NOT trade. It proves the foundation works:
config loads and validates, the DB schema applies, and boot is logged.
The state machine, market data, strategy, risk engine, filters, and
execution modules are built in later phases and wired in here then —
see architecture doc §11 for the phase plan.
"""

from __future__ import annotations

import argparse
import sys

from config.config_schema import load_config
from core.enums import EventType
from core.events import BotEvent
from persistence.db_manager import DBManager
from persistence.logger import EventLogger


def main() -> int:
    parser = argparse.ArgumentParser(description="XAUUSD bot — Phase 0 boot")
    parser.add_argument(
        "--config",
        default="config/environments/backtest.yaml",
        help="Path to environment config YAML",
    )
    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except Exception as exc:  # noqa: BLE001 — boot-time config errors are fatal and reported, not swallowed
        print(f"FATAL: config failed validation: {exc}", file=sys.stderr)
        return 1

    db = DBManager(f"data/{config.environment}.db")
    db.init_schema()
    event_logger = EventLogger(db, log_file=f"logs/{config.environment}.log")

    event_logger.log(
        BotEvent(
            event_type=EventType.BOOT,
            message=f"Bot booted in '{config.environment}' environment",
            payload={
                "symbol": config.broker.symbol,
                "default_risk_percent": config.risk.default_risk_percent,
                "max_risk_percent": config.risk.max_risk_percent,
                "max_drawdown_percent": config.risk.max_drawdown_percent,
                "account_currency_unit": config.broker.account_currency_unit,
            },
        )
    )

    print(f"Boot OK — environment={config.environment}, config validated, schema applied.")
    print("No trading logic exists yet (Phase 0). See architecture doc for the phase plan.")

    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

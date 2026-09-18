using cAlgo.API;

namespace cAlgo.Robots;

// =====================================================================
// XAUUSDcBot — parameter declarations (C# side of the Python cBot).
//
// cTrader Python algos declare customisable parameters in this .cs
// file (the .NET engine requires it); the Python code in
// "XAUUSD cBot_main.py" reads them through the `api` global, e.g.
// `api.DefaultRiskPercent`.
//
// Every value below mirrors xauusd_bot/config/config.yaml defaults and
// bounds. Python-side validation in config_schema.py re-checks ALL of
// these at boot and fails loudly (Stop()) on any violation — the cTrader
// Min/Max here is only UI convenience, the schema is the authority.
// =====================================================================
[Robot(AccessRights = AccessRights.None, AddIndicators = true)]
public partial class XAUUSDcBot : Robot
{
    // === Runtime ===

    [Parameter("Timeframe (minutes — must match this cBot's chart TF)", DefaultValue = 15, Group = "Runtime", MinValue = 1)]
    public int TimeframeMinutes { get; set; }

    [Parameter("Position label (used to find this bot's positions)", DefaultValue = "XAUUSDcBot", Group = "Runtime")]
    public string Label { get; set; }

    [Parameter("Audit dir (empty = auto: Documents/XAUUSDcBot-audit/<account>/<label>)", DefaultValue = "", Group = "Runtime")]
    public string AuditDir { get; set; }

    [Parameter("Strict persistence (halt trading if audit write fails)", DefaultValue = true, Group = "Runtime")]
    public bool StrictPersistence { get; set; }

    [Parameter("Preload history candles at start (0 = use what's loaded)", DefaultValue = 1500, Group = "Runtime", MinValue = 0)]
    public int HistoryPreloadCandles { get; set; }

    // === Risk (xauusd_bot RiskConfig bounds) ===

    [Parameter("Default risk per trade (fraction, 0.0025-0.005)", DefaultValue = 0.0035, Group = "Risk", MinValue = 0.0025, MaxValue = 0.005, Step = 0.0001)]
    public double DefaultRiskPercent { get; set; }

    [Parameter("Max risk per trade (fraction, hard ceiling 0.01)", DefaultValue = 0.01, Group = "Risk", MinValue = 0.0001, MaxValue = 0.01, Step = 0.0001)]
    public double MaxRiskPercent { get; set; }

    [Parameter("Daily loss limit (fraction, 0.02 = 2%)", DefaultValue = 0.02, Group = "Risk", MinValue = 0.0001, MaxValue = 0.05, Step = 0.0001)]
    public double DailyLossLimitPercent { get; set; }

    [Parameter("Max drawdown from peak equity (fraction, 0.10 = 10%)", DefaultValue = 0.10, Group = "Risk", MinValue = 0.0001, MaxValue = 0.25, Step = 0.0001)]
    public double MaxDrawdownPercent { get; set; }

    [Parameter("Max consecutive losses before cooldown", DefaultValue = 3, Group = "Risk", MinValue = 1, MaxValue = 10, Step = 1)]
    public int MaxConsecutiveLosses { get; set; }

    [Parameter("Consecutive-loss cooldown (hours)", DefaultValue = 24, Group = "Risk", MinValue = 1, Step = 1)]
    public int ConsecutiveLossCooldownHours { get; set; }

    [Parameter("Margin utilization cap (fraction of free margin per trade)", DefaultValue = 0.5, Group = "Risk", MinValue = 0.0001, MaxValue = 1.0, Step = 0.01)]
    public double MarginUtilizationCap { get; set; }

    [Parameter("Min-lot risk tolerance (0 = strict reject; max 0.002)", DefaultValue = 0.0, Group = "Risk", MinValue = 0.0, MaxValue = 0.002, Step = 0.0001)]
    public double MinLotRiskTolerance { get; set; }

    // === Filters (xauusd_bot FilterConfig) ===

    [Parameter("Max spread (points; SET FROM OBSERVED LIVE XM XAUUSD SPREAD)", DefaultValue = 35, Group = "Filters", MinValue = 0.01, Step = 0.5)]
    public double MaxSpreadPoints { get; set; }

    [Parameter("Duplicate-order debounce (seconds)", DefaultValue = 5, Group = "Filters", MinValue = 1, Step = 1)]
    public int DuplicateOrderDebounceSeconds { get; set; }

    [Parameter("Allowed sessions (empty = always; e.g. MON-FRI:01:00-23:58 or pipe-separated; UTC)", DefaultValue = "", Group = "Filters")]
    public string AllowedSessions { get; set; }

    // === Broker (XM, cTrader, Raw Spread standard; verify in Symbol Info) ===

    [Parameter("Expected symbol (validated against the chart's symbol)", DefaultValue = "XAUUSD", Group = "Broker")]
    public string ExpectedSymbol { get; set; }

    [Parameter("Leverage", DefaultValue = 100, Group = "Broker", MinValue = 1, MaxValue = 100, Step = 1)]
    public int Leverage { get; set; }

    [Parameter("Contract size (oz per lot; 100 for XAUUSD)", DefaultValue = 100.0, Group = "Broker", MinValue = 0.001, Step = 1.0)]
    public double ContractSize { get; set; }

    [Parameter("Price digits (point position)", DefaultValue = 2, Group = "Broker", MinValue = 0, MaxValue = 6, Step = 1)]
    public int Digits { get; set; }

    [Parameter("Lot step", DefaultValue = 0.01, Group = "Broker", MinValue = 0.0001, Step = 0.01)]
    public double LotStep { get; set; }

    [Parameter("Min lot", DefaultValue = 0.01, Group = "Broker", MinValue = 0.0001, Step = 0.01)]
    public double MinLot { get; set; }

    [Parameter("Max lot", DefaultValue = 100.0, Group = "Broker", MinValue = 0.01, Step = 0.01)]
    public double MaxLot { get; set; }

    [Parameter("Margin rate", DefaultValue = 1.0, Group = "Broker", MinValue = 0.001, Step = 0.1)]
    public double MarginRate { get; set; }

    [Parameter("Commission ($ per $1M notional, per side — verify vs a filled order)", DefaultValue = 30.0, Group = "Broker", MinValue = 0, Step = 0.5)]
    public double CommissionPerMillionUsd { get; set; }

    [Parameter("Account currency unit (usd or cents)", DefaultValue = "usd", Group = "Broker")]
    public string AccountCurrencyUnit { get; set; }

    [Parameter("Unit scale factor (>1 required for cents accounts)", DefaultValue = 1.0, Group = "Broker", MinValue = 0.001, Step = 0.1)]
    public double UnitScaleFactor { get; set; }

    [Parameter("Swap enabled (FLIP ONLY AFTER CONFIRMING THIS ACCOUNT'S SWAP STATUS)", DefaultValue = false, Group = "Broker")]
    public bool SwapEnabled { get; set; }

    [Parameter("Swap long (points)", DefaultValue = -58.6, Group = "Broker", Step = 0.1)]
    public double SwapLongPoints { get; set; }

    [Parameter("Swap short (points)", DefaultValue = 40.9, Group = "Broker", Step = 0.1)]
    public double SwapShortPoints { get; set; }

    // === Kill switch (xauusd_bot KillSwitchConfig) ===

    [Parameter("Manual trigger enabled (NOT supported in this port; kept for parity)", DefaultValue = false, Group = "Kill Switch")]
    public bool ManualTriggerEnabled { get; set; }

    [Parameter("Manual rearm required (after any trip, you must re-add the cBot)", DefaultValue = true, Group = "Kill Switch")]
    public bool ManualRearmRequired { get; set; }

    [Parameter("Trigger on broker disconnect (inert in-cTrader: no separate API link)", DefaultValue = true, Group = "Kill Switch")]
    public bool TriggerOnBrokerDisconnect { get; set; }

    [Parameter("Trigger on max drawdown (mark-to-market equity)", DefaultValue = true, Group = "Kill Switch")]
    public bool TriggerOnMaxDrawdown { get; set; }

    // === Strategy selection ===

    // One of:
    //   "trend_pullback_breakout_v2"       (recommended default; xauusd_bot strategy)
    //   "trend_pullback_h1_m15"            (v1; xauusd_bot strategy)
    //   "mean_reversion_v1"                (xauusd_bot strategy)
    //   "placeholder_alternating_TEST_ONLY" (DETERMINISTIC TEST STRATEGY ONLY)
    [Parameter("Strategy", DefaultValue = "trend_pullback_breakout_v2", Group = "Strategy")]
    public string Strategy { get; set; }

    // Trailing stop (applies to positions opened WITHOUT a fixed take-profit)
    [Parameter("Trailing stop enabled (ATR-based; for TP-less signals)", DefaultValue = false, Group = "Strategy")]
    public bool TrailingStopEnabled { get; set; }

    [Parameter("Trailing stop ATR multiplier", DefaultValue = 3.0, Group = "Strategy", MinValue = 0.1, Step = 0.1)]
    public double TrailingStopAtrMultiplier { get; set; }

    [Parameter("Trailing stop ATR period", DefaultValue = 14, Group = "Strategy", MinValue = 1, MaxValue = 200, Step = 1)]
    public int TrailingStopAtrPeriod { get; set; }

    // --- placeholder_alternating_TEST_ONLY parameters ---
    [Parameter("PH: signal interval (candles)", DefaultValue = 10, Group = "Strategy/placeholder", MinValue = 1, Step = 1)]
    public int PhInterval { get; set; }

    [Parameter("PH: stop distance (price units)", DefaultValue = 10.0, Group = "Strategy/placeholder", MinValue = 0.01, Step = 0.1)]
    public double PhStopDistance { get; set; }

    [Parameter("PH: take-profit distance (0 = none -> trailing)", DefaultValue = 20.0, Group = "Strategy/placeholder", MinValue = 0, Step = 0.1)]
    public double PhTakeProfitDistance { get; set; }

    // --- trend_pullback_h1_m15 parameters ---
    [Parameter("TP: HTF minutes (resample)", DefaultValue = 60, Group = "Strategy/trend_pullback", MinValue = 15, Step = 15)]
    public int TpHtfMinutes { get; set; }

    [Parameter("TP: HTF fast EMA period", DefaultValue = 50, Group = "Strategy/trend_pullback", MinValue = 2, Step = 1)]
    public int TpHtfFastEma { get; set; }

    [Parameter("TP: HTF slow EMA period", DefaultValue = 200, Group = "Strategy/trend_pullback", MinValue = 2, Step = 1)]
    public int TpHtfSlowEma { get; set; }

    [Parameter("TP: LTF fast EMA period", DefaultValue = 20, Group = "Strategy/trend_pullback", MinValue = 2, Step = 1)]
    public int TpLtfFastEma { get; set; }

    [Parameter("TP: ATR period", DefaultValue = 14, Group = "Strategy/trend_pullback", MinValue = 1, Step = 1)]
    public int TpAtrPeriod { get; set; }

    [Parameter("TP: stop = ATR x multiplier", DefaultValue = 1.5, Group = "Strategy/trend_pullback", MinValue = 0.1, Step = 0.1)]
    public double TpAtrStopMult { get; set; }

    [Parameter("TP: reward:risk", DefaultValue = 2.0, Group = "Strategy/trend_pullback", MinValue = 0.1, Step = 0.1)]
    public double TpRewardRisk { get; set; }

    [Parameter("TP: RSI period", DefaultValue = 14, Group = "Strategy/trend_pullback", MinValue = 1, Step = 1)]
    public int TpRsiPeriod { get; set; }

    [Parameter("TP: RSI long min", DefaultValue = 40.0, Group = "Strategy/trend_pullback", MinValue = 0, MaxValue = 100, Step = 1)]
    public double TpRsiLongMin { get; set; }

    [Parameter("TP: RSI long max", DefaultValue = 75.0, Group = "Strategy/trend_pullback", MinValue = 0, MaxValue = 100, Step = 1)]
    public double TpRsiLongMax { get; set; }

    [Parameter("TP: RSI short min", DefaultValue = 25.0, Group = "Strategy/trend_pullback", MinValue = 0, MaxValue = 100, Step = 1)]
    public double TpRsiShortMin { get; set; }

    [Parameter("TP: RSI short max", DefaultValue = 60.0, Group = "Strategy/trend_pullback", MinValue = 0, MaxValue = 100, Step = 1)]
    public double TpRsiShortMax { get; set; }

    [Parameter("TP: min ATR (0 = no volatility floor)", DefaultValue = 0.0, Group = "Strategy/trend_pullback", MinValue = 0, Step = 0.01)]
    public double TpMinAtrPrice { get; set; }

    // --- trend_pullback_breakout_v2 parameters ---
    [Parameter("T2: HTF minutes (resample)", DefaultValue = 60, Group = "Strategy/trend_pullback_breakout", MinValue = 15, Step = 15)]
    public int T2HtfMinutes { get; set; }

    [Parameter("T2: HTF fast EMA period", DefaultValue = 50, Group = "Strategy/trend_pullback_breakout", MinValue = 2, Step = 1)]
    public int T2HtfFastEma { get; set; }

    [Parameter("T2: HTF slow EMA period", DefaultValue = 200, Group = "Strategy/trend_pullback_breakout", MinValue = 2, Step = 1)]
    public int T2HtfSlowEma { get; set; }

    [Parameter("T2: HTF ATR period", DefaultValue = 14, Group = "Strategy/trend_pullback_breakout", MinValue = 1, Step = 1)]
    public int T2HtfAtrPeriod { get; set; }

    [Parameter("T2: min trend strength (HTF ATR multiples)", DefaultValue = 1.0, Group = "Strategy/trend_pullback_breakout", MinValue = 0.0, Step = 0.1)]
    public double T2MinTrendStrengthAtr { get; set; }

    [Parameter("T2: LTF fast EMA period", DefaultValue = 20, Group = "Strategy/trend_pullback_breakout", MinValue = 2, Step = 1)]
    public int T2LtfFastEma { get; set; }

    [Parameter("T2: min pullback bars", DefaultValue = 2, Group = "Strategy/trend_pullback_breakout", MinValue = 1, Step = 1)]
    public int T2MinPullbackBars { get; set; }

    [Parameter("T2: max pullback bars", DefaultValue = 8, Group = "Strategy/trend_pullback_breakout", MinValue = 1, Step = 1)]
    public int T2MaxPullbackBars { get; set; }

    [Parameter("T2: ATR period", DefaultValue = 14, Group = "Strategy/trend_pullback_breakout", MinValue = 1, Step = 1)]
    public int T2AtrPeriod { get; set; }

    [Parameter("T2: stop buffer (ATR multiples beyond pullback extreme)", DefaultValue = 0.3, Group = "Strategy/trend_pullback_breakout", MinValue = 0.0, Step = 0.05)]
    public double T2StopBufferAtrMult { get; set; }

    [Parameter("T2: reward:risk", DefaultValue = 2.0, Group = "Strategy/trend_pullback_breakout", MinValue = 0.1, Step = 0.1)]
    public double T2RewardRisk { get; set; }

    [Parameter("T2: RSI period", DefaultValue = 14, Group = "Strategy/trend_pullback_breakout", MinValue = 1, Step = 1)]
    public int T2RsiPeriod { get; set; }

    [Parameter("T2: RSI long min", DefaultValue = 30.0, Group = "Strategy/trend_pullback_breakout", MinValue = 0, MaxValue = 100, Step = 1)]
    public double T2RsiLongMin { get; set; }

    [Parameter("T2: RSI long max", DefaultValue = 75.0, Group = "Strategy/trend_pullback_breakout", MinValue = 0, MaxValue = 100, Step = 1)]
    public double T2RsiLongMax { get; set; }

    [Parameter("T2: RSI short min", DefaultValue = 25.0, Group = "Strategy/trend_pullback_breakout", MinValue = 0, MaxValue = 100, Step = 1)]
    public double T2RsiShortMin { get; set; }

    [Parameter("T2: RSI short max", DefaultValue = 70.0, Group = "Strategy/trend_pullback_breakout", MinValue = 0, MaxValue = 100, Step = 1)]
    public double T2RsiShortMax { get; set; }

    [Parameter("T2: use trailing stop (omit fixed TP)", DefaultValue = false, Group = "Strategy/trend_pullback_breakout")]
    public bool T2UseTrailingStop { get; set; }

    // --- mean_reversion_v1 parameters ---
    [Parameter("MR: EMA period", DefaultValue = 20, Group = "Strategy/mean_reversion", MinValue = 2, Step = 1)]
    public int MrEmaPeriod { get; set; }

    [Parameter("MR: ATR period", DefaultValue = 14, Group = "Strategy/mean_reversion", MinValue = 1, Step = 1)]
    public int MrAtrPeriod { get; set; }

    [Parameter("MR: entry threshold (ATR multiples)", DefaultValue = 2.0, Group = "Strategy/mean_reversion", MinValue = 0.1, Step = 0.1)]
    public double MrEntryThresholdAtr { get; set; }

    [Parameter("MR: stop = ATR x multiplier", DefaultValue = 1.5, Group = "Strategy/mean_reversion", MinValue = 0.1, Step = 0.1)]
    public double MrStopAtrMult { get; set; }

    [Parameter("MR: HTF minutes (0 = off)", DefaultValue = 0, Group = "Strategy/mean_reversion", MinValue = 0, Step = 15)]
    public int MrHtfMinutes { get; set; }

    [Parameter("MR: HTF fast EMA period", DefaultValue = 50, Group = "Strategy/mean_reversion", MinValue = 2, Step = 1)]
    public int MrHtfFastEma { get; set; }

    [Parameter("MR: HTF slow EMA period", DefaultValue = 200, Group = "Strategy/mean_reversion", MinValue = 2, Step = 1)]
    public int MrHtfSlowEma { get; set; }

    [Parameter("MR: HTF ATR period", DefaultValue = 14, Group = "Strategy/mean_reversion", MinValue = 1, Step = 1)]
    public int MrHtfAtrPeriod { get; set; }

    [Parameter("MR: max HTF trend strength to fade (0 = off)", DefaultValue = 0.0, Group = "Strategy/mean_reversion", MinValue = 0.0, Step = 0.1)]
    public double MrMaxHtfTrendStrength { get; set; }
}

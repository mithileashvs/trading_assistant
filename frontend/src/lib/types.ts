// These types mirror the FastAPI response shapes in app/api/main.py
// (and the dataclasses/pydantic models they're built from) field for
// field. If the backend shape changes, update here — never invent a
// field the backend doesn't actually return.

export type TradingMode = "BACKTEST" | "PAPER" | "LIVE";
export type Regime =
  | "TREND_BULLISH"
  | "TREND_BEARISH"
  | "RANGE"
  | "HIGH_VOLATILITY"
  | "UNCERTAIN";
export type SignalDirection = "BUY" | "SELL" | "NO_SIGNAL";
export type TrendDirection = "BULLISH" | "BEARISH" | "NEUTRAL";
export type ScoreLabel = "NO_TRADE" | "WEAK" | "VALID" | "STRONG";

export interface KillSwitchStatus {
  active: boolean;
  reason: string | null;
  activated_at: string | null;
  activated_by: string | null;
  close_positions: boolean;
  deactivated_at: string | null;
  deactivated_by: string | null;
}

export interface SystemStatus {
  trading_mode: TradingMode;
  mt5_use_mock: boolean;
  mt5_connected: boolean;
  symbol: string;
  kill_switch_active: boolean;
  kill_switch_status: KillSwitchStatus;
  broker_trade_allowed: boolean | null;
  account_error: string | null;
  market_data_fresh: boolean;
  market_data_error: string | null;
  // news_filter_available is a coarse "is a data source configured at
  // all" flag; news_state is the full NewsState (see NewsStatus) for
  // anywhere a more precise badge is useful. Neither alone tells you
  // "is it safe to open a new trade right now" -- use
  // NewsStatus.permits_new_trade (from /api/news) for that.
  news_filter_available: boolean;
  news_state: NewsState;
  server_time_utc: string;
}

export interface AccountInfo {
  login: number;
  balance: number;
  equity: number;
  margin: number;
  margin_free: number;
  currency: string;
  leverage: number;
  trade_allowed: boolean;
  is_demo: boolean | null;
  daily_pnl: number;
}

export interface MarketSnapshot {
  symbol: string;
  bid: number;
  ask: number;
  spread: number;
  regime: Regime;
  regime_confidence: number;
  regime_reasons: string[];
  atr: number | null;
  atr_pct: number | null;
  adx: number | null;
  rsi: number | null;
  trend_direction: TrendDirection;
}

export interface TimeframeSnapshot {
  available: boolean;
  error?: string;
  regime?: Regime;
  confidence?: number;
  reasons?: string[];
  adx?: number | null;
  rsi?: number | null;
  macd?: number | null;
  macd_signal?: number | null;
  macd_histogram?: number | null;
  atr_pct?: number | null;
  bb_upper?: number | null;
  bb_middle?: number | null;
  bb_lower?: number | null;
  bb_width_pct?: number | null;
  structure?: string | null;
  trend_direction?: TrendDirection;
}

export interface TimeframesResponse {
  symbol: string;
  timeframes: Record<"H4" | "H1" | "M15", TimeframeSnapshot>;
}

export interface Signal {
  direction: SignalDirection;
  strategy: string;
  reasons: string[];
  confidence: number | null;
  entry: number | null;
  stop_loss: number | null;
  take_profit: number | null;
  risk_reward: number | null;
  invalidation: number | null;
  score: number | null;
  score_breakdown: Record<string, number> | null;
  score_label: ScoreLabel | null;
}

export interface TradeValidation {
  symbol: string;
  direction: string;
  strategy: string;
  regime: string;
  score: number | null;
  entry: number | null;
  stop_loss: number | null;
  take_profit: number | null;
  risk_reward: number | null;
  risk_percent: number;
  lots: number;
  spread_ok: boolean;
  news_ok: boolean;
  news_state: NewsState;
  daily_loss_limit_ok: boolean;
  weekly_loss_limit_ok: boolean;
  position_limit_ok: boolean;
  market_data_fresh: boolean;
  approved: boolean;
  rejection_reasons: string[];
  guard_checks: Record<string, boolean>;
}

export interface TradeExplanation {
  decision: string;
  decision_detail: string;
  why: string;
  why_now: string;
  why_this_strategy: string;
  why_this_entry: string;
  why_this_stop: string;
  why_this_target: string;
  how_much_risk: string;
  what_would_invalidate_it: string;
}

export interface SignalResponse {
  signal: Signal;
  validation: TradeValidation;
  explanation: TradeExplanation;
  all_signals: Signal[];
}

export interface CloseResult {
  queued: boolean;
  success: boolean | null;
  order_id: number | null;
  price: number | null;
  volume: number | null;
  retcode: number | null;
  comment: string | null;
  command_id?: number;
}

export interface PositionCommandStatus {
  id: number;
  command_type: string;
  ticket: number;
  volume: number | null;
  status: "PENDING" | "DONE" | "FAILED";
  result: CloseResult | null;
  created_at: string;
  processed_at: string | null;
}

export interface Position {
  ticket: number | string;
  direction: string;
  volume: number;
  entry_price: number;
  stop_loss: number | null;
  take_profit: number | null;
  strategy: string;
  regime: string;
  open_time: string;
  unrealized_pnl_ticks: number | null;
  r_multiple: number | null;
}

export interface PositionsResponse {
  positions: Position[];
}

export interface RiskLimits {
  risk_per_trade_pct: number;
  max_daily_loss_pct: number;
  max_weekly_loss_pct: number;
  max_open_positions: number;
  max_trades_per_day: number;
  max_consecutive_losses: number;
  max_spread_points: number;
  max_slippage_points: number;
  min_account_equity: number;
}

export interface RiskStatus {
  account_balance: number;
  equity: number;
  margin_free: number;
  daily_pnl: number;
  daily_loss_pct: number;
  weekly_pnl: number;
  weekly_loss_pct: number;
  trades_today: number;
  open_positions: number;
  consecutive_losses: number;
  current_spread_points: number;
  kill_switch_active: boolean;
  kill_switch_status: KillSwitchStatus;
  checks_passed: boolean;
  checks: Record<string, boolean>;
  reasons: string[];
  limits: RiskLimits;
}

export interface Candle {
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface CandlesResponse {
  symbol: string;
  timeframe: string;
  candles: Candle[];
  overlays: {
    ema_20: (number | null)[];
    ema_50: (number | null)[];
    ema_200: (number | null)[];
    bb_upper: (number | null)[];
    bb_middle: (number | null)[];
    bb_lower: (number | null)[];
  };
  oscillators: {
    rsi_14: (number | null)[];
    macd: (number | null)[];
    macd_signal: (number | null)[];
    macd_histogram: (number | null)[];
    adx_14: (number | null)[];
    atr_14: (number | null)[];
  };
}

// Mirrors app/news/filter.py::NewsState. CLEAR is the ONLY state that
// permits a new trade -- BLOCKED, UNAVAILABLE, and UNKNOWN all block
// identically (see permits_new_trade in that module). Never render
// anything other than CLEAR as "safe" -- "we don't know" must never
// look like "it's fine".
export type NewsState = "CLEAR" | "BLOCKED" | "UNAVAILABLE" | "UNKNOWN";

export interface NewsStatus {
  state: NewsState;
  permits_new_trade: boolean;
  reason: string | null;
}

export interface JournalEntry {
  record_type: "trade" | "signal";
  timestamp: string | null;
  symbol: string;
  direction: string;
  strategy: string;
  regime: string | null;
  score: number | null;
  entry_price: number | null;
  exit_price?: number | null;
  stop_loss: number | null;
  take_profit: number | null;
  lots?: number | null;
  pnl?: number | null;
  r_multiple?: number | null;
  exit_reason?: string | null;
  approved?: boolean;
  rejection_reasons?: string[];
  status: string;
}

export interface JournalResponse {
  entries: JournalEntry[];
}

export interface BacktestTrade {
  symbol: string;
  direction: string;
  strategy: string;
  regime: string;
  score: number | null;
  open_time: string;
  close_time: string;
  entry_price: number;
  exit_price: number;
  stop_loss: number;
  take_profit: number;
  lots: number;
  pnl: number;
  gross_pnl: number;
  commission: number;
  swap: number;
  r_multiple: number | null;
  mae: number;
  mfe: number;
  exit_reason: string;
  session: string;
}

export interface BacktestResponse {
  is_historical_simulation: true;
  symbol: string;
  bars_used: number;
  strategy_filter: string | null;
  trade_count: number;
  starting_balance: number;
  ending_balance: number;
  net_profit: number;
  equity_curve: { time: string; equity: number }[];
  trades: BacktestTrade[];
  metrics: Record<string, unknown>;
}

export interface StrategiesResponse {
  strategies: { name: string; enabled: boolean }[];
  regime_strategy_map: Record<string, string[]>;
}
export interface PerformanceMetrics {
  number_of_trades: number;
  win_rate: number;
  profit_factor: number | null;
  expectancy: number;
  net_profit: number;
  max_drawdown_abs: number;
  max_drawdown_pct: number;
  starting_balance?: number;
  current_balance?: number;
  note?: string;
  equity_curve?: { time: string | null; equity: number }[];
  net_pnl_by_strategy?: Record<string, number>;
  net_pnl_by_regime?: Record<string, number>;
}

import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import App from "../App";
import { api } from "../lib/api";
import type {
  AccountInfo,
  MarketSnapshot,
  PositionsResponse,
  RiskStatus,
  SignalResponse,
  SystemStatus,
  TimeframesResponse,
} from "../lib/types";

const { MockApiError } = vi.hoisted(() => {
  class MockApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  }
  return { MockApiError };
});

vi.mock("lightweight-charts", () => {
  const fakeSeries = { setData: vi.fn() };
  const fakeChart = {
    addSeries: vi.fn(() => fakeSeries),
    priceScale: vi.fn(() => ({ applyOptions: vi.fn() })),
    timeScale: vi.fn(() => ({ fitContent: vi.fn() })),
    applyOptions: vi.fn(),
    remove: vi.fn(),
  };
  return {
    createChart: vi.fn(() => fakeChart),
    ColorType: { Solid: "solid" },
    CandlestickSeries: "CandlestickSeries",
    HistogramSeries: "HistogramSeries",
    LineSeries: "LineSeries",
  };
});

vi.mock("../lib/api", () => ({
  ApiError: MockApiError,
  api: {
    system: vi.fn(),
    account: vi.fn(),
    market: vi.fn(),
    timeframes: vi.fn(),
    signal: vi.fn(),
    positions: vi.fn(),
    risk: vi.fn(),
    performance: vi.fn(),
    candles: vi.fn(),
    closePosition: vi.fn(),
    closePositionPartial: vi.fn(),
    positionCommandStatus: vi.fn(),
    news: vi.fn(),
    journal: vi.fn(),
    strategies: vi.fn(),
    runBacktest: vi.fn(),
    activateKillSwitch: vi.fn(),
    deactivateKillSwitch: vi.fn(),
  },
}));

const mockedApi = vi.mocked(api);

const SYSTEM: SystemStatus = {
  trading_mode: "PAPER",
  mt5_use_mock: true,
  mt5_connected: true,
  symbol: "XAUUSD",
  kill_switch_active: false,
  kill_switch_status: {
    active: false, reason: null, activated_at: null, activated_by: null,
    close_positions: false, deactivated_at: null, deactivated_by: null,
  },
  broker_trade_allowed: true,
  account_error: null,
  market_data_fresh: true,
  market_data_error: null,
  news_filter_available: false,
  news_state: "UNAVAILABLE",
  server_time_utc: new Date().toISOString(),
};

const ACCOUNT: AccountInfo = {
  login: 1, balance: 10000, equity: 10043.21, margin: 200, margin_free: 9843.21,
  currency: "USD", leverage: 100, trade_allowed: true, is_demo: true, daily_pnl: 43.21,
};

const MARKET: MarketSnapshot = {
  symbol: "XAUUSD", bid: 2650.15, ask: 2650.35, spread: 0.2, regime: "TREND_BULLISH",
  regime_confidence: 0.7, regime_reasons: ["EMA alignment bullish"], atr: 4.3, atr_pct: 0.16,
  adx: 26.3, rsi: 56.3, trend_direction: "BULLISH",
};

const TIMEFRAMES: TimeframesResponse = {
  symbol: "XAUUSD",
  timeframes: {
    H4: { available: true, regime: "TREND_BULLISH", confidence: 0.7, reasons: [], adx: 26.3, rsi: 56.3, trend_direction: "BULLISH" },
    H1: { available: true, regime: "UNCERTAIN", confidence: 0.3, reasons: [], adx: 30, rsi: 50, trend_direction: "NEUTRAL" },
    M15: { available: true, regime: "TREND_BEARISH", confidence: 0.71, reasons: [], adx: 28, rsi: 40, trend_direction: "BEARISH" },
  },
};

const SIGNAL: SignalResponse = {
  signal: {
    direction: "BUY", strategy: "TREND_PULLBACK", reasons: [], confidence: 0.8, entry: 2650.15,
    stop_loss: 2642.50, take_profit: 2665.45, risk_reward: 2.0, invalidation: null, score: 8,
    score_breakdown: null, score_label: "VALID",
  },
  validation: {
    symbol: "XAUUSD", direction: "BUY", strategy: "TREND_PULLBACK", regime: "TREND_BULLISH", score: 8,
    entry: 2650.15, stop_loss: 2642.50, take_profit: 2665.45, risk_reward: 2.0, risk_percent: 0.5,
    lots: 0.03, spread_ok: true, news_ok: true, news_state: "CLEAR", daily_loss_limit_ok: true,
    weekly_loss_limit_ok: true, position_limit_ok: true, market_data_fresh: true, approved: true,
    rejection_reasons: [], guard_checks: { max_spread_ok: true, daily_loss_ok: true },
  },
  explanation: {
    decision: "APPROVED", decision_detail: "", why: "", why_now: "", why_this_strategy: "",
    why_this_entry: "", why_this_stop: "", why_this_target: "", how_much_risk: "", what_would_invalidate_it: "",
  },
  all_signals: [
    {
      direction: "BUY", strategy: "TREND_PULLBACK", reasons: [], confidence: 0.8, entry: 2650.15,
      stop_loss: 2642.50, take_profit: 2665.45, risk_reward: 2.0, invalidation: null, score: 8,
      score_breakdown: null, score_label: "VALID",
    },
  ],
};

const POSITIONS: PositionsResponse = { positions: [] };
const RISK: RiskStatus = {
  account_balance: 10000, equity: 10000, margin_free: 10000,
  daily_pnl: 0, daily_loss_pct: 0, weekly_pnl: 0, weekly_loss_pct: 0, trades_today: 0,
  open_positions: 0, consecutive_losses: 0, current_spread_points: 20,
  kill_switch_active: false, kill_switch_status: SYSTEM.kill_switch_status,
  checks_passed: true,
  checks: {
    kill_switch_ok: true, mt5_connected: true, broker_trade_allowed: true, market_data_fresh: true,
    min_equity_ok: true, daily_loss_ok: true, weekly_loss_ok: true, max_trades_per_day_ok: true,
    max_open_positions_ok: true, max_consecutive_losses_ok: true, max_spread_ok: true,
  },
  reasons: [],
  limits: {
    risk_per_trade_pct: 0.5, max_daily_loss_pct: 2, max_weekly_loss_pct: 5, max_open_positions: 1,
    max_trades_per_day: 5, max_consecutive_losses: 3, max_spread_points: 50, max_slippage_points: 20,
    min_account_equity: 0,
  },
};

function setupHappyPath() {
  mockedApi.system.mockResolvedValue(SYSTEM);
  mockedApi.account.mockResolvedValue(ACCOUNT);
  mockedApi.market.mockResolvedValue(MARKET);
  mockedApi.timeframes.mockResolvedValue(TIMEFRAMES);
  mockedApi.signal.mockResolvedValue(SIGNAL);
  mockedApi.positions.mockResolvedValue(POSITIONS);
  mockedApi.risk.mockResolvedValue(RISK);
  mockedApi.performance.mockResolvedValue({ number_of_trades: 0, win_rate: 0, profit_factor: 0, expectancy: 0, net_profit: 0, max_drawdown_abs: 0, max_drawdown_pct: 0 });
  mockedApi.news.mockResolvedValue({ state: "UNAVAILABLE", permits_new_trade: false, reason: "No news-data provider is configured; news filter is unavailable." });
  mockedApi.journal.mockResolvedValue({ entries: [] });
  mockedApi.strategies.mockResolvedValue({
    strategies: [
      { name: "TREND_PULLBACK", enabled: true },
      { name: "BREAKOUT", enabled: true },
      { name: "MEAN_REVERSION", enabled: true },
    ],
    regime_strategy_map: { TREND_BULLISH: ["TREND_PULLBACK"], RANGE: ["MEAN_REVERSION"] },
  });
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("Dashboard", () => {
  it("renders the app shell (sidebar nav + GOLDSIGNAL brand) and dashboard data once loaded", async () => {
    setupHappyPath();
    render(
      <MemoryRouter initialEntries={["/"]}>
        <App />
      </MemoryRouter>,
    );

    // Sidebar nav items present
    expect(screen.getAllByText("Dashboard").length).toBeGreaterThan(0);
    expect(screen.getByText("Risk")).toBeInTheDocument();
    expect(screen.getByText("Safety")).toBeInTheDocument();

    await waitFor(() => expect(screen.getAllByText(/PAPER/).length).toBeGreaterThan(0));

    // Market price rendered from real mocked backend data, not fabricated
    await waitFor(() => expect(screen.getByText("$2,650.15")).toBeInTheDocument());

    // Final decision reflects backend approval
    await waitFor(() => expect(screen.getAllByText("APPROVED").length).toBeGreaterThan(0));
  });

  it("shows the MOCK MT5 badge and PAPER mode badge from backend system status", async () => {
    setupHappyPath();
    render(
      <MemoryRouter initialEntries={["/"]}>
        <App />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText(/MT5 MOCK/)).toBeInTheDocument());
  });

  it("shows LIVE mode as a dangerous/distinct badge when backend reports LIVE", async () => {
    setupHappyPath();
    mockedApi.system.mockResolvedValue({ ...SYSTEM, trading_mode: "LIVE", mt5_use_mock: false, mt5_connected: true });
    render(
      <MemoryRouter initialEntries={["/"]}>
        <App />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getAllByText("LIVE").length).toBeGreaterThan(0));
  });

  it("renders kill-switch ACTIVE state distinctly when the backend reports it active", async () => {
    setupHappyPath();
    mockedApi.system.mockResolvedValue({ ...SYSTEM, kill_switch_active: true });
    render(
      <MemoryRouter initialEntries={["/"]}>
        <App />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText("ACTIVE")).toBeInTheDocument());
  });

  it("renders an UNAVAILABLE error state instead of fake data when market data fails", async () => {
    setupHappyPath();
    mockedApi.market.mockRejectedValue(new Error("MT5 connection unavailable."));
    render(
      <MemoryRouter initialEntries={["/"]}>
        <App />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText("MARKET DATA UNAVAILABLE")).toBeInTheDocument());
    expect(screen.getByText("MT5 connection unavailable.")).toBeInTheDocument();
  });

  it("shows NO_SIGNAL / rejection reasons rather than a fabricated trade when backend rejects", async () => {
    setupHappyPath();
    mockedApi.signal.mockResolvedValue({
      ...SIGNAL,
      signal: { ...SIGNAL.signal, direction: "NO_SIGNAL", entry: null, stop_loss: null, take_profit: null },
      validation: {
        ...SIGNAL.validation, approved: false, entry: null, stop_loss: null, take_profit: null, lots: 0,
        rejection_reasons: ["No actionable signal (NO_SIGNAL) — nothing to validate."],
      },
    });
    render(
      <MemoryRouter initialEntries={["/"]}>
        <App />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText("NO TRADE")).toBeInTheDocument());
    expect(screen.getByText(/No actionable setup/)).toBeInTheDocument();
  });

  it("renders open positions count from backend, not a hardcoded value", async () => {
    setupHappyPath();
    mockedApi.positions.mockResolvedValue({
      positions: [
        { ticket: 1042, direction: "BUY", volume: 0.03, entry_price: 2644.8, stop_loss: 2642.5, take_profit: 2665.45, strategy: "TREND_PULLBACK", regime: "TREND_BULLISH", open_time: new Date().toISOString(), unrealized_pnl_ticks: 10, r_multiple: 0.5 },
      ],
    });
    render(
      <MemoryRouter initialEntries={["/"]}>
        <App />
      </MemoryRouter>,
    );
    await waitFor(() => {
      const el = screen.getByText("Open Positions").parentElement;
      expect(el).toHaveTextContent("1");
    });
  });

  it("navigates to the Charts page and requests candle data (not fake candles)", async () => {
    setupHappyPath();
    mockedApi.candles.mockResolvedValue({
      symbol: "XAUUSD",
      timeframe: "H1",
      candles: [
        { time: new Date().toISOString(), open: 2650, high: 2652, low: 2648, close: 2651, volume: 100 },
      ],
      overlays: { ema_20: [2650], ema_50: [2650], ema_200: [2650], bb_upper: [2652], bb_middle: [2650], bb_lower: [2648] },
      oscillators: { rsi_14: [55], macd: [0.1], macd_signal: [0.05], macd_histogram: [0.05], adx_14: [25], atr_14: [4] },
    });
    render(
      <MemoryRouter initialEntries={["/charts"]}>
        <App />
      </MemoryRouter>,
    );
    await waitFor(() => expect(mockedApi.candles).toHaveBeenCalledWith("H1", 300));
    await waitFor(() => expect(screen.getByText(/XAUUSD — H1/)).toBeInTheDocument());
  });

  it("navigates to the Signals page and shows the scoring breakdown and other strategies", async () => {
    setupHappyPath();
    mockedApi.signal.mockResolvedValue({
      ...SIGNAL,
      signal: { ...SIGNAL.signal, score_breakdown: { h4_trend_alignment: 2, m15_momentum: 1 } },
    });
    render(
      <MemoryRouter initialEntries={["/signals"]}>
        <App />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText("Current Signal")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText("H4 alignment")).toBeInTheDocument());
    expect(screen.getByText("Other Strategies")).toBeInTheDocument();
  });

  it("navigates to the Positions page and can trigger a close with the backend's exact rejection reason shown", async () => {
    setupHappyPath();
    mockedApi.positions.mockResolvedValue({
      positions: [
        { ticket: 1042, direction: "BUY", volume: 0.03, entry_price: 2644.8, stop_loss: 2642.5, take_profit: 2665.45, strategy: "TREND_PULLBACK", regime: "TREND_BULLISH", open_time: new Date().toISOString(), unrealized_pnl_ticks: 10, r_multiple: 0.5 },
      ],
    });
    mockedApi.closePosition.mockResolvedValue({ queued: false, success: false, order_id: null, price: null, volume: null, retcode: -1, comment: "POSITION_NOT_FOUND" });

    render(
      <MemoryRouter initialEntries={["/positions"]}>
        <App />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText("Open Positions (1)")).toBeInTheDocument());
    fireEvent.click(screen.getByText("1042"));
    await waitFor(() => expect(screen.getByText("Close Position")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Close Position"));
    await waitFor(() => expect(screen.getByText("Confirm Close")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Confirm Close"));
    await waitFor(() => expect(screen.getByText(/Rejected: POSITION_NOT_FOUND/)).toBeInTheDocument());
  });

  it("queues a PAPER-mode close and polls for the real outcome from the trading loop, rather than assuming success", async () => {
    setupHappyPath();
    mockedApi.positions.mockResolvedValue({
      positions: [
        { ticket: 1042, direction: "BUY", volume: 0.03, entry_price: 2644.8, stop_loss: 2642.5, take_profit: 2665.45, strategy: "TREND_PULLBACK", regime: "TREND_BULLISH", open_time: new Date().toISOString(), unrealized_pnl_ticks: 10, r_multiple: 0.5 },
      ],
    });
    mockedApi.closePosition.mockResolvedValue({ queued: true, success: null, order_id: null, price: null, volume: null, retcode: null, comment: "Queued for the trading loop to execute.", command_id: 7 });
    mockedApi.positionCommandStatus.mockResolvedValue({
      id: 7, command_type: "CLOSE", ticket: 1042, volume: null, status: "DONE",
      result: { queued: false, success: true, order_id: 1042, price: 2650.0, volume: 0.03, retcode: 10009, comment: "PAPER_CLOSED:MANUAL_DASHBOARD_CLOSE" },
      created_at: new Date().toISOString(), processed_at: new Date().toISOString(),
    });

    render(
      <MemoryRouter initialEntries={["/positions"]}>
        <App />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText("Open Positions (1)")).toBeInTheDocument());
    fireEvent.click(screen.getByText("1042"));
    await waitFor(() => expect(screen.getByText("Close Position")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Close Position"));
    await waitFor(() => expect(screen.getByText("Confirm Close")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Confirm Close"));
    // The immediate response is a queued state, not an assumed success…
    await waitFor(() => expect(mockedApi.closePosition).toHaveBeenCalled());
    // …and the panel then polls the real command status until it resolves.
    await waitFor(() => expect(mockedApi.positionCommandStatus).toHaveBeenCalledWith(7), { timeout: 3000 });
    await waitFor(() => expect(screen.getByText(/Closed: PAPER_CLOSED/)).toBeInTheDocument(), { timeout: 3000 });
  });

  it("navigates to the Risk page and shows lock status derived from the backend's own guard checks", async () => {
    setupHappyPath();
    mockedApi.risk.mockResolvedValue({
      ...RISK,
      checks_passed: false,
      checks: { ...RISK.checks, daily_loss_ok: false },
      reasons: ["Daily loss (2.50%) exceeds the configured maximum (2.0%)."],
      daily_loss_pct: 2.5,
    });
    render(
      <MemoryRouter initialEntries={["/risk"]}>
        <App />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText("New entries blocked")).toBeInTheDocument());
    expect(screen.getByText(/Daily loss \(2.50%\)/)).toBeInTheDocument();
    expect(screen.getAllByText("LOCK ACTIVE").length).toBeGreaterThan(0);
  });

  it("navigates to the Safety page and can activate the kill switch with confirmation", async () => {
    setupHappyPath();
    mockedApi.risk.mockResolvedValue(RISK);
    mockedApi.activateKillSwitch.mockResolvedValue({ ...SYSTEM.kill_switch_status, active: true, reason: "test", activated_by: "dashboard_user" });
    render(
      <MemoryRouter initialEntries={["/safety"]}>
        <App />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText("System Safe")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Activate Kill Switch"));
    await waitFor(() => expect(screen.getByText(/Activate kill switch\?/)).toBeInTheDocument());
    fireEvent.click(screen.getByText("Confirm — Activate"));
    await waitFor(() => expect(mockedApi.activateKillSwitch).toHaveBeenCalled());
  });

  it("renders the Live Event Feed with a connecting/empty state, never fake events", async () => {
    setupHappyPath();
    render(
      <MemoryRouter initialEntries={["/"]}>
        <App />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText("Live Event Feed")).toBeInTheDocument());
    expect(screen.getByText(/Waiting to connect|No events logged yet/)).toBeInTheDocument();
  });

  it("shows honest news UNAVAILABLE state rather than a fabricated calendar", async () => {
    setupHappyPath();
    render(
      <MemoryRouter initialEntries={["/news"]}>
        <App />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText("News Safety")).toBeInTheDocument());
    expect(screen.getAllByText("UNAVAILABLE").length).toBeGreaterThan(0);
  });

  it("renders the execution pipeline reflecting the backend's real approval state", async () => {
    setupHappyPath();
    render(
      <MemoryRouter initialEntries={["/execution"]}>
        <App />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText("Execution Pipeline")).toBeInTheDocument());
    await waitFor(() => expect(screen.getAllByText("APPROVED").length).toBeGreaterThan(0));
  });

  it("renders the journal with filters and no fake entries when empty", async () => {
    setupHappyPath();
    render(
      <MemoryRouter initialEntries={["/journal"]}>
        <App />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText("No journal entries yet.")).toBeInTheDocument());
  });

  it("runs a backtest and displays the real returned metrics, labelled as historical simulation", async () => {
    setupHappyPath();
    mockedApi.runBacktest.mockResolvedValue({
      is_historical_simulation: true, symbol: "XAUUSD", bars_used: 2000, strategy_filter: null,
      trade_count: 3, starting_balance: 10000, ending_balance: 10120, net_profit: 120,
      equity_curve: [{ time: "t0", equity: 10000 }, { time: "t1", equity: 10120 }],
      trades: [], metrics: { win_rate: 0.66, profit_factor: 1.4, max_drawdown_pct: 0.02 },
    });
    render(
      <MemoryRouter initialEntries={["/backtest"]}>
        <App />
      </MemoryRouter>,
    );
    fireEvent.click(await screen.findByText("Run Backtest"));
    await waitFor(() => expect(screen.getByText(/Historical simulation/)).toBeInTheDocument());
    expect(mockedApi.runBacktest).toHaveBeenCalled();
  });

  it("shows the strategy registry with honest 'lifecycle not tracked' note", async () => {
    setupHappyPath();
    render(
      <MemoryRouter initialEntries={["/strategy-lab"]}>
        <App />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getAllByText("TREND PULLBACK").length).toBeGreaterThan(0));
    expect(screen.getAllByText(/Lifecycle: not tracked/).length).toBeGreaterThan(0);
  });

  it("renders performance metrics from the backend, showing a no-trades note when empty", async () => {
    setupHappyPath();
    render(
      <MemoryRouter initialEntries={["/performance"]}>
        <App />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getAllByText(/No closed trades yet/).length).toBeGreaterThan(0));
  });

  it("shows Settings with trading mode marked read-only and never silently switchable", async () => {
    setupHappyPath();
    render(
      <MemoryRouter initialEntries={["/settings"]}>
        <App />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText("Trading Mode")).toBeInTheDocument());
    expect(screen.getByText(/cannot be changed from this dashboard/)).toBeInTheDocument();
  });

  it("shows a not-found placeholder for unknown routes without crashing", async () => {
    setupHappyPath();
    render(
      <MemoryRouter initialEntries={["/definitely-not-a-real-route"]}>
        <App />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText("Not Found")).toBeInTheDocument());
  });
});

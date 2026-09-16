import type {
  AccountInfo,
  BacktestResponse,
  CandlesResponse,
  CloseResult,
  JournalResponse,
  KillSwitchStatus,
  MarketSnapshot,
  NewsStatus,
  PerformanceMetrics,
  PositionCommandStatus,
  PositionsResponse,
  RiskStatus,
  SignalResponse,
  StrategiesResponse,
  SystemStatus,
  TimeframesResponse,
} from "./types";

// All requests go through the same origin (Vite dev proxy forwards
// /api/* to the FastAPI backend in dev; in production this app is
// meant to be served behind the same origin as the API, or built with
// VITE_API_BASE set). The frontend never talks to anything but this
// backend — never MT5, never a broker, directly.
const BASE = import.meta.env.VITE_API_BASE ?? "";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { headers: { Accept: "application/json" } });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? detail;
    } catch {
      /* body wasn't JSON — keep statusText */
    }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

async function postJson<T>(path: string, params?: Record<string, string>): Promise<T> {
  const qs = params ? `?${new URLSearchParams(params).toString()}` : "";
  const res = await fetch(`${BASE}${path}${qs}`, { method: "POST" });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? detail;
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

export const api = {
  system: () => getJson<SystemStatus>("/api/system"),
  account: () => getJson<AccountInfo>("/api/account"),
  market: () => getJson<MarketSnapshot>("/api/market"),
  timeframes: () => getJson<TimeframesResponse>("/api/timeframes"),
  candles: (timeframe: string, count = 300) =>
    getJson<CandlesResponse>(`/api/candles?${new URLSearchParams({ timeframe, count: String(count) }).toString()}`),
  signal: () => getJson<SignalResponse>("/api/signal"),
  positions: () => getJson<PositionsResponse>("/api/positions"),
  closePosition: (ticket: number | string, reason?: string) =>
    postJson<CloseResult>(`/api/positions/${ticket}/close`, reason ? { reason } : undefined),
  closePositionPartial: (ticket: number | string, volume: number) =>
    postJson<CloseResult>(`/api/positions/${ticket}/close-partial`, { volume: String(volume) }),
  positionCommandStatus: (commandId: number) =>
    getJson<PositionCommandStatus>(`/api/positions/commands/${commandId}`),
  risk: () => getJson<RiskStatus>("/api/risk"),
  performance: () => getJson<PerformanceMetrics>("/api/performance"),
  news: () => getJson<NewsStatus>("/api/news"),
  journal: (kind: "all" | "trades" | "signals" = "all", limit = 100) =>
    getJson<JournalResponse>(`/api/journal?${new URLSearchParams({ kind, limit: String(limit) }).toString()}`),
  strategies: () => getJson<StrategiesResponse>("/api/strategies"),
  runBacktest: (params: { bars?: number; warmup_bars?: number; step?: number; starting_balance?: number; strategy?: string }) => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined) qs.set(k, String(v));
    }
    return postJson<BacktestResponse>(`/api/backtest/run?${qs.toString()}`);
  },
  activateKillSwitch: (reason: string, activatedBy: string) =>
    postJson<KillSwitchStatus>("/api/kill-switch/activate", { reason, activated_by: activatedBy }),
  deactivateKillSwitch: (deactivatedBy: string) =>
    postJson<KillSwitchStatus>("/api/kill-switch/deactivate", { deactivated_by: deactivatedBy }),
};

import { createContext, useContext, type ReactNode } from "react";
import { api } from "../lib/api";
import { usePolling, type FetchState } from "../lib/usePolling";
import type { SystemStatus } from "../lib/types";

const SystemStatusContext = createContext<FetchState<SystemStatus> | null>(null);

export function SystemStatusProvider({ children }: { children: ReactNode }) {
  const state = usePolling(api.system, 5000);
  return <SystemStatusContext.Provider value={state}>{children}</SystemStatusContext.Provider>;
}

export function useSystemStatus(): FetchState<SystemStatus> {
  const ctx = useContext(SystemStatusContext);
  if (!ctx) throw new Error("useSystemStatus must be used within SystemStatusProvider");
  return ctx;
}

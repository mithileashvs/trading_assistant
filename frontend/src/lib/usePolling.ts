import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "./api";

export type FetchState<T> = {
  data: T | null;
  error: string | null;
  loading: boolean;
  lastUpdated: number | null;
  refresh: () => void;
};

/**
 * Polls `fetcher` on an interval and exposes loading/error/data/lastUpdated.
 * On error, previous `data` is kept (so the UI can show "stale" rather
 * than a jarring blank state) but `error` is set so callers can render
 * an explicit UNAVAILABLE / stale banner rather than silently reusing
 * old numbers as if they were current.
 */
export function usePolling<T>(fetcher: () => Promise<T>, intervalMs: number): FetchState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const tick = useCallback(async () => {
    try {
      const result = await fetcherRef.current();
      setData(result);
      setError(null);
      setLastUpdated(Date.now());
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : e instanceof Error ? e.message : "Request failed";
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;

    async function loop() {
      if (cancelled) return;
      await tick();
      if (cancelled) return;
      timer = setTimeout(loop, intervalMs);
    }
    loop();

    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [tick, intervalMs]);

  return { data, error, loading, lastUpdated, refresh: tick };
}

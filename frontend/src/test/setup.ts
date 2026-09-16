import "@testing-library/jest-dom/vitest";

// jsdom doesn't implement ResizeObserver; charts (lightweight-charts) use it.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver = globalThis.ResizeObserver ?? ResizeObserverStub;

// jsdom doesn't implement WebSocket either; the live event feed uses it.
class WebSocketStub {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(_url: string) {
    // Never auto-opens in tests — components should render their
    // "connecting" state cleanly without a real server.
  }
  close() {
    this.onclose?.();
  }
  send() {}
}
globalThis.WebSocket = globalThis.WebSocket ?? WebSocketStub;

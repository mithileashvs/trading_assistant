import { lazy, Suspense } from "react";
import { Route, Routes } from "react-router-dom";
import { AppShell } from "./components/layout/AppShell";
import { SystemStatusProvider } from "./lib/SystemStatusContext";
import { LoadingState } from "./components/ui/States";
import { PlaceholderPage } from "./pages/PlaceholderPage";

// Route-level code splitting: the charting library (lightweight-charts)
// and the backtest/strategy-lab pages are the heaviest chunks and are
// rarely needed on first paint (the Dashboard is), so they're loaded
// on demand rather than bundled into the initial JS payload.
const DashboardPage = lazy(() => import("./pages/DashboardPage").then((m) => ({ default: m.DashboardPage })));
const ChartsPage = lazy(() => import("./pages/ChartsPage").then((m) => ({ default: m.ChartsPage })));
const SignalsPage = lazy(() => import("./pages/SignalsPage").then((m) => ({ default: m.SignalsPage })));
const PositionsPage = lazy(() => import("./pages/PositionsPage").then((m) => ({ default: m.PositionsPage })));
const RiskPage = lazy(() => import("./pages/RiskPage").then((m) => ({ default: m.RiskPage })));
const SafetyPage = lazy(() => import("./pages/SafetyPage").then((m) => ({ default: m.SafetyPage })));
const NewsPage = lazy(() => import("./pages/NewsPage").then((m) => ({ default: m.NewsPage })));
const ExecutionPage = lazy(() => import("./pages/ExecutionPage").then((m) => ({ default: m.ExecutionPage })));
const BacktestPage = lazy(() => import("./pages/BacktestPage").then((m) => ({ default: m.BacktestPage })));
const StrategyLabPage = lazy(() => import("./pages/StrategyLabPage").then((m) => ({ default: m.StrategyLabPage })));
const JournalPage = lazy(() => import("./pages/JournalPage").then((m) => ({ default: m.JournalPage })));
const PerformancePage = lazy(() => import("./pages/PerformancePage").then((m) => ({ default: m.PerformancePage })));
const SettingsPage = lazy(() => import("./pages/SettingsPage").then((m) => ({ default: m.SettingsPage })));

function App() {
  return (
    <SystemStatusProvider>
      <AppShell>
        <Suspense fallback={<LoadingState label="Loading page…" />}>
          <Routes>
            <Route path="/" element={<DashboardPage />} />
            <Route path="/charts" element={<ChartsPage />} />
            <Route path="/signals" element={<SignalsPage />} />
            <Route path="/positions" element={<PositionsPage />} />
            <Route path="/risk" element={<RiskPage />} />
            <Route path="/safety" element={<SafetyPage />} />
            <Route path="/news" element={<NewsPage />} />
            <Route path="/execution" element={<ExecutionPage />} />
            <Route path="/backtest" element={<BacktestPage />} />
            <Route path="/strategy-lab" element={<StrategyLabPage />} />
            <Route path="/journal" element={<JournalPage />} />
            <Route path="/history" element={<JournalPage />} />
            <Route path="/performance" element={<PerformancePage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="*" element={<PlaceholderPage title="Not Found" stage="—" />} />
          </Routes>
        </Suspense>
      </AppShell>
    </SystemStatusProvider>
  );
}

export default App;

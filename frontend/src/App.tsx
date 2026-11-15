import { useEffect, lazy, Suspense } from 'react';
import { Routes, Route, Navigate, useLocation } from 'react-router-dom';
import { AdaptiveLayout } from './components/Layout';
import { ToastContainer } from './components/Toast';
import { useAuthStore } from './store/authStore';
import { useMarketsStore } from './store/marketsStore';

// 路由级代码拆分：主 bundle 不再携带全部页面。页面全部 lazy——首次进入
// 时按需拉 chunk（CF 边缘 <300ms），登录页用户不再下载钱包/订单/行情页
// 的代码，/trade 常客也不会为不用的页面付下载和解析成本。
const TradePage = lazy(() => import('./pages/TradePage').then(m => ({ default: m.TradePage })));
const MarketsPage = lazy(() => import('./pages/MarketsPage').then(m => ({ default: m.MarketsPage })));
const WalletPage = lazy(() => import('./pages/WalletPage').then(m => ({ default: m.WalletPage })));
const AssetDetailPage = lazy(() => import('./pages/AssetDetailPage').then(m => ({ default: m.AssetDetailPage })));
const OrdersPage = lazy(() => import('./pages/OrdersPage').then(m => ({ default: m.OrdersPage })));
const SettingsPage = lazy(() => import('./pages/SettingsPage').then(m => ({ default: m.SettingsPage })));
const AuthPage = lazy(() => import('./pages/AuthPage').then(m => ({ default: m.AuthPage })));

function PageFallback() {
  // 极简加载态：chunk 拉取通常 <300ms，不值得上骨架屏。
  return <div className="page-loading" aria-busy="true" />;
}

// Private Route Component
const PrivateRoute = ({ children }: { children: React.ReactNode }) => {
  const { isAuthenticated } = useAuthStore();
  return isAuthenticated ? <Suspense fallback={<PageFallback />}>{children}</Suspense> : <Navigate to="/auth" replace />;
};

// Auth page wrapper - standalone without layout
function AuthPageWrapper() {
  return (
    <div className="app-container">
      <Suspense fallback={<PageFallback />}>
        <AuthPage />
      </Suspense>
      <ToastContainer />
    </div>
  );
}

export function App() {
  const location = useLocation();
  const { isAuthenticated } = useAuthStore();
  const loadMarkets = useMarketsStore((s) => s.loadMarkets);

  // Boot: exchange a legacy stored token for an HttpOnly session cookie
  // (once), then probe the cookie session. A dead session flows through the
  // unauthorized event into the login gate below.
  useEffect(() => {
    void useAuthStore.getState().establishSession();
  }, []);

  // 市场注册表（币种/每手/最小跳动/涨跌停/时段）是全局唯一来源，登录后拉取一次。
  // 时段钟随 payload 一起来，消费方（如 Watchlist 轮询降频）依赖它判断开收盘；
  // 每 5 分钟刷新一次让"收盘→开盘"的节奏切换在一个轮询周期内生效。
  useEffect(() => {
    if (!isAuthenticated) return;
    void loadMarkets();
    const interval = setInterval(() => { void loadMarkets(); }, 300_000);
    return () => clearInterval(interval);
  }, [isAuthenticated, loadMarkets]);

  // A 401 from any API call clears the token; reflect that immediately and
  // wipe client-side risk state with it — armed triggers must not keep
  // firing under whatever session logs in next (2026-11-16 review).
  useEffect(() => {
    const onUnauthorized = () => {
      import('./store/automationStore').then(({ useAutomationStore }) => {
        useAutomationStore.getState().resetForLogout();
      });
      import('./store/notificationStore').then(({ useNotificationStore }) => {
        useNotificationStore.getState().clear();
      });
      useAuthStore.setState({ isAuthenticated: false });
    };
    window.addEventListener('schwab:unauthorized', onUnauthorized);
    return () => window.removeEventListener('schwab:unauthorized', onUnauthorized);
  }, []);

  // Auth page is rendered standalone without the adaptive layout
  if (location.pathname === '/auth') {
    return <AuthPageWrapper />;
  }

  // Redirect to auth if not authenticated
  if (!isAuthenticated) {
    return <Navigate to="/auth" replace />;
  }

  return (
    <Routes>
      <Route element={<AdaptiveLayout />}>
        <Route path="/trade" element={<PrivateRoute><TradePage /></PrivateRoute>} />
        <Route path="/markets" element={<PrivateRoute><MarketsPage /></PrivateRoute>} />
        <Route path="/wallet" element={<PrivateRoute><WalletPage /></PrivateRoute>} />
        <Route path="/assets" element={<PrivateRoute><AssetDetailPage /></PrivateRoute>} />
        <Route path="/orders" element={<PrivateRoute><OrdersPage /></PrivateRoute>} />
        <Route path="/settings" element={<PrivateRoute><SettingsPage /></PrivateRoute>} />
        <Route path="/" element={<Navigate to="/trade" replace />} />
        <Route path="*" element={<Navigate to="/trade" replace />} />
      </Route>
    </Routes>
  );
}

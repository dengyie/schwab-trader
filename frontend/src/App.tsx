import { useEffect } from 'react';
import { Routes, Route, Navigate, useLocation } from 'react-router-dom';
import { AdaptiveLayout } from './components/Layout';
import { ToastContainer } from './components/Toast';
import { TradePage } from './pages/TradePage';
import { MarketsPage } from './pages/MarketsPage';
import { WalletPage } from './pages/WalletPage';
import { AssetDetailPage } from './pages/AssetDetailPage';
import { OrdersPage } from './pages/OrdersPage';
import { SettingsPage } from './pages/SettingsPage';
import { AuthPage } from './pages/AuthPage';
import { useAuthStore } from './store/authStore';
import { useMarketsStore } from './store/marketsStore';

// Private Route Component
const PrivateRoute = ({ children }: { children: React.ReactNode }) => {
  const { isAuthenticated } = useAuthStore();
  return isAuthenticated ? <>{children}</> : <Navigate to="/auth" replace />;
};

// Auth page wrapper - standalone without layout
function AuthPageWrapper() {
  return (
    <div className="app-container">
      <AuthPage />
      <ToastContainer />
    </div>
  );
}

export function App() {
  const location = useLocation();
  const { isAuthenticated } = useAuthStore();
  const loadMarkets = useMarketsStore((s) => s.loadMarkets);

  // 市场注册表（币种/每手/最小跳动/涨跌停/时段）是全局唯一来源，登录后拉取一次。
  // 时段钟随 payload 一起来，消费方（如 Watchlist 轮询降频）依赖它判断开收盘；
  // 每 5 分钟刷新一次让"收盘→开盘"的节奏切换在一个轮询周期内生效。
  useEffect(() => {
    if (!isAuthenticated) return;
    void loadMarkets();
    const interval = setInterval(() => { void loadMarkets(); }, 300_000);
    return () => clearInterval(interval);
  }, [isAuthenticated, loadMarkets]);

  // A 401 from any API call clears the token; reflect that immediately.
  useEffect(() => {
    const onUnauthorized = () => useAuthStore.setState({ isAuthenticated: false });
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

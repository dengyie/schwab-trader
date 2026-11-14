import { useEffect } from 'react';
import { useIsMobile } from '../../hooks/useMediaQuery';
import { useWalletStore } from '../../store/walletStore';
import { useMarketStore } from '../../store/marketStore';
import { MobileLayout } from './MobileLayout';
import { DesktopLayout } from './DesktopLayout';

/**
 * AdaptiveLayout - Root layout component that switches between Mobile and Desktop layouts
 * This component completely decouples the mobile and desktop DOM trees for optimal performance
 */
export function AdaptiveLayout() {
  const isMobile = useIsMobile();
  const fetchPortfolio = useWalletStore((state) => state.fetchPortfolio);
  const fetchMarketClock = useMarketStore((state) => state.fetchMarketClock);
  // 收盘判定用市场时钟；时钟缺失时保守按"开盘"节奏（多轮询好过漏更新）。
  const marketOpen = useMarketStore((s) => s.marketClock?.is_open !== false);

  // 后台标签页降载：隐藏时掐断 WS 行情流（心跳/指标批/stale 快照在
  // 无人看的页面全是空转），回到前台原符号原 token 重连。HTTP 轮询
  // 各自已有 document.hidden 门控；这里统一管 WS 这一条。
  useEffect(() => {
    const { pauseFeed, resumeFeed } = useMarketStore.getState();
    const onVisibility = () => {
      if (document.hidden) pauseFeed();
      else resumeFeed();
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      document.removeEventListener('visibilitychange', onVisibility);
      // 卸载时若还停在后台，恢复订阅避免 worker 永久挂起。
      if (document.hidden) useMarketStore.getState().resumeFeed();
    };
  }, []);

  // Portfolio 轮询双节奏（与 Watchlist 行情轮询同一时钟依据）：开盘中
  // 30s；收盘 120s——收盘后持仓是定格快照，每 30s 打一次 ~1s 的
  // /api/portfolio（Alpaca + 汇率链）是纯空转。
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    let stopped = false;
    const pace = () => (marketOpen ? 30000 : 120000);
    const tick = async () => {
      await fetchPortfolio();
      if (!stopped) timer = setTimeout(tick, pace());
    };
    void tick();
    return () => { stopped = true; if (timer) clearTimeout(timer); };
  }, [fetchPortfolio, marketOpen]);

  // Session clock decides "market closed" vs "feed broken". The broker flips
  // it at most twice a day, so a slow poll is plenty.
  useEffect(() => {
    fetchMarketClock();
    const interval = setInterval(fetchMarketClock, 60000);
    return () => clearInterval(interval);
  }, [fetchMarketClock]);

  // Render completely different component trees for mobile vs desktop
  // This prevents unnecessary re-renders and allows for optimized layouts
  if (isMobile) {
    return <MobileLayout />;
  }

  return <DesktopLayout />;
}


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

  useEffect(() => {
    fetchPortfolio();
    const interval = setInterval(fetchPortfolio, 30000);
    return () => clearInterval(interval);
  }, [fetchPortfolio]);

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


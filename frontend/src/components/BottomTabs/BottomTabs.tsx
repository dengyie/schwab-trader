import { useState, useCallback, useEffect } from 'react';
import { Positions } from '../Positions';
import { OpenOrders } from '../OpenOrders';
import { OrderBook } from '../OrderBook';
import { TriggerForm } from '../AutomationPanel/TriggerForm';
import { TriggerList } from '../AutomationPanel/TriggerList';
import { AiStrategyRadar } from '../AiStrategyRadar';
import { useTradingStore } from '../../store/tradingStore';
import { useAutomationStore } from '../../store/automationStore';
import { useWatchlistStore, selectSelectedSymbol } from '../../store/watchlistStore';
import { Icon } from '../Icon';
import { apiFetch } from '../../services/apiClient';
import styles from './BottomTabs.module.css';

type LeftTab = 'positions' | 'orders' | 'orderbook';
type RightTab = 'radar' | 'create' | 'triggers';

interface BottomTabsProps {
  onPriceClick?: (price: string, side?: 'buy' | 'sell') => void;
}

function AutomationStatusBar() {
  const triggers = useAutomationStore((state) => state.triggers);
  const armedCount = triggers.filter(t => t.enabled && t.status === 'armed').length;
  const pausedCount = triggers.filter(t => !t.enabled || t.status === 'paused').length;
  const triggeredCount = triggers.filter(t => t.status === 'triggered' || t.status === 'completed').length;

  return (
    <div className={styles.statusBar}>
      <div className={styles.statusItem}>
        <span className={`${styles.statusDot} ${armedCount > 0 ? styles.active : ''}`} />
        <span className={styles.statusLabel}>Armed</span>
        <span className={styles.statusValue}>{armedCount}</span>
      </div>
      <div className={styles.statusItem}>
        <span className={`${styles.statusDot} ${styles.paused}`} />
        <span className={styles.statusLabel}>Paused</span>
        <span className={styles.statusValue}>{pausedCount}</span>
      </div>
      <div className={styles.statusItem}>
        <span className={styles.statusLabel}>Executed</span>
        <span className={styles.statusValue}>{triggeredCount}</span>
      </div>
    </div>
  );
}

export function BottomTabs({ onPriceClick }: BottomTabsProps) {
  const [leftTab, setLeftTab] = useState<LeftTab>('positions');
  const [rightTab, setRightTab] = useState<RightTab>('radar');
  const [modelContext, setModelContext] = useState<any>(null);
  const [radarOpened, setRadarOpened] = useState(false);

  const openOrdersCount = useTradingStore((state) => state.getOpenOrders().length);
  const positionsCount = Array.from(useTradingStore((state) => state.positions.entries())).filter(([_, pos]) => parseFloat(pos.quantity) > 0).length;
  const triggersCount = useAutomationStore((state) => state.triggers.filter(t => t.enabled).length);
  const selectedSymbol = useWatchlistStore(selectSelectedSymbol);
  const fetchOrders = useTradingStore((state) => state.fetchOrders);

  useEffect(() => {
    fetchOrders();
  }, [fetchOrders]);

  // model-context 只在雷达页签打开过才拉：后端这条要跑 quote + bars +
  // broker.get_position 全链，不看的页签白打一次是纯空转。切换标的时
  // 若雷达已开过，重新拉取让推荐跟当前符号走。
  useEffect(() => {
    if (!radarOpened || !selectedSymbol) return;
    apiFetch(`/market/model-context?symbol=${encodeURIComponent(selectedSymbol)}`)
      .then(r => r.ok ? r.json() : null)
      .then(d => d && setModelContext(d))
      .catch(() => {});
  }, [radarOpened, selectedSymbol]);

  const handlePriceClick = useCallback((price: string, side?: 'buy' | 'sell') => {
    onPriceClick?.(price, side);
  }, [onPriceClick]);

  return (
    <div className={styles.container}>
      {/* Two Column Layout */}
      <div className={styles.columns}>
        {/* Left Column - Positions/Orders */}
        <div className={styles.leftColumn}>
          <div className={styles.columnHeader}>
            <div className={styles.tabs}>
              <button 
                className={`${styles.tab} ${leftTab === 'positions' ? styles.active : ''}`}
                onClick={() => setLeftTab('positions')}
              >
                <Icon name="briefcase" size="xs" />
                <span>Positions</span>
                {positionsCount > 0 && <span className={styles.badge}>{positionsCount}</span>}
              </button>
              <button 
                className={`${styles.tab} ${leftTab === 'orders' ? styles.active : ''}`}
                onClick={() => setLeftTab('orders')}
              >
                <Icon name="list" size="xs" />
                <span>Orders</span>
                {openOrdersCount > 0 && <span className={styles.badge}>{openOrdersCount}</span>}
              </button>
              <button 
                className={`${styles.tab} ${styles.orderbookTab} ${leftTab === 'orderbook' ? styles.active : ''}`}
                onClick={() => setLeftTab('orderbook')}
              >
                <Icon name="bar-chart-2" size="xs" />
                <span>Book</span>
              </button>
            </div>
          </div>
          <div className={styles.columnContent}>
            {leftTab === 'positions' && <Positions />}
            {leftTab === 'orders' && <OpenOrders />}
            {leftTab === 'orderbook' && <OrderBook onPriceClick={handlePriceClick} embedded />}
          </div>
        </div>

        {/* Right Column - Automation & AI Radar */}
        <div className={styles.rightColumn}>
          <div className={styles.columnHeader}>
            <div className={styles.tabs}>
              <button
                className={`${styles.tab} ${rightTab === 'radar' ? styles.active : ''}`}
                onClick={() => { setRightTab('radar'); setRadarOpened(true); }}
              >
                <Icon name="cpu" size="xs" />
                <span>AI Radar</span>
              </button>
              <button 
                className={`${styles.tab} ${rightTab === 'create' ? styles.active : ''}`}
                onClick={() => setRightTab('create')}
              >
                <Icon name="plus" size="xs" />
                <span>New</span>
              </button>
              <button 
                className={`${styles.tab} ${rightTab === 'triggers' ? styles.active : ''}`}
                onClick={() => setRightTab('triggers')}
              >
                <Icon name="zap" size="xs" />
                <span>Triggers</span>
                {triggersCount > 0 && <span className={styles.badge}>{triggersCount}</span>}
              </button>
            </div>
          </div>
          <div className={styles.columnContent}>
            {rightTab === 'radar' && (
              <AiStrategyRadar
                modelContext={modelContext}
                onApplyRecommendation={(rec) => {
                  handlePriceClick(rec.price.toFixed(2), rec.side.toLowerCase() as 'buy' | 'sell');
                }}
              />
            )}
            {rightTab === 'create' && (
              <TriggerForm onSuccess={() => setRightTab('triggers')} compact />
            )}
            {rightTab === 'triggers' && (
              <TriggerList filterSymbol={selectedSymbol} compact />
            )}
          </div>
          <AutomationStatusBar />
        </div>
      </div>
    </div>
  );
}

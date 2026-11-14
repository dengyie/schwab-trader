import { useState, useEffect } from 'react';
import { useMarketStore, selectConnectionStatus, selectDataConfidence, selectOrderBook } from '../../store/marketStore';
import { useI18n } from '../../i18n';
import { formatLastUpdateTime } from '../../utils/timeFormat';
import { DiagnosticsDrawer } from './DiagnosticsDrawer';
import { Icon } from '../Icon';
import styles from './DataConfidenceBar.module.css';

export function DataConfidenceBar() {
  const { t } = useI18n();
  const connectionStatus = useMarketStore(selectConnectionStatus);
  const dataConfidence = useMarketStore(selectDataConfidence);
  const orderBook = useMarketStore(selectOrderBook);
  const [showDrawer, setShowDrawer] = useState(false);
  const [lastUpdateTime, setLastUpdateTime] = useState('');

  const { level, reason } = dataConfidence;
  const isClosed = level === 'closed';

  // While the venue is closed, WS messages are just keepalives/snapshots: the
  // honest "last update" is the quote's own timestamp, which stays frozen at
  // the closing print instead of dancing with the local clock. Without a venue
  // timestamp there is nothing honest to show, so render "—" rather than the
  // receive time of a keepalive.
  const displayTimestamp = isClosed
    ? (orderBook?.quoteTime ?? 0)
    : connectionStatus.lastMessageTime;

  // 时效标签的刷新节奏分档：前 60 秒秒针在跳，1s 跟得上；进入分钟档
  // 后 5s 足够；更旧 30s 只是兜底。后台标签页完全停止——回前台时
  // visibilitychange 立即补刷一次再恢复节奏。
  useEffect(() => {
    if (displayTimestamp <= 0) {
      setLastUpdateTime('—');
      return;
    }
    const refresh = () => setLastUpdateTime(formatLastUpdateTime(displayTimestamp));
    refresh();
    const age = () => Date.now() - displayTimestamp;
    const pace = () => (age() < 60_000 ? 1000 : age() < 3600_000 ? 5000 : 30000);
    let timer: ReturnType<typeof setInterval> | undefined;
    const start = () => {
      clearInterval(timer);
      timer = setInterval(refresh, pace());
    };
    start();
    const onVisibility = () => {
      if (document.hidden) clearInterval(timer);
      else { refresh(); start(); }
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [displayTimestamp]);

  const getStatusText = () => {
    switch (level) {
      case 'live': return t.dataConfidence.live;
      case 'degraded': return t.dataConfidence.degraded;
      case 'resyncing': return t.dataConfidence.resyncing;
      case 'stale': return t.dataConfidence.stale;
      // 收盘是正常状态，不是故障：行情本就该静止
      case 'closed': return t.dataConfidence.closed;
      default: return '';
    }
  };

  // Transport latency and message rate measure a live feed. After the close
  // they only reflect keepalives, so reporting them would claim the market is
  // moving.
  const getLatencyDisplay = () => {
    if (isClosed) return '—';
    if (connectionStatus.state !== 'connected') return '—';
    if (connectionStatus.latencyMs < 1) return '<1ms';
    return `${Math.round(connectionStatus.latencyMs)}ms`;
  };

  const getMessageRateDisplay = () => {
    if (isClosed) return '—';
    return `${connectionStatus.messageRate}/s`;
  };

  return (
    <>
      <div className={`${styles.bar} ${styles[level]} animate-fade`}>
        <div className={styles.statusSection}>
          <div className={styles.statusInfo}>
            <span className={styles.statusText}>{getStatusText()}</span>
            {reason && <span className={styles.statusReason}>{reason}</span>}
          </div>
        </div>

        <div className={styles.divider} />

        <div className={styles.metricsSection}>
          <div className={styles.metric}>
            <Icon name="clock" size="xs" className={styles.metricIcon} />
            <span className="tabular-nums">{lastUpdateTime}</span>
          </div>
          <div className={styles.metric}>
            <Icon name="zap" size="xs" className={styles.metricIcon} />
            <span className="tabular-nums">{getLatencyDisplay()}</span>
          </div>
          <div className={styles.metric}>
            <Icon name="bar-chart-2" size="xs" className={styles.metricIcon} />
            <span className="tabular-nums">{getMessageRateDisplay()}</span>
          </div>
        </div>

        <button
          className={styles.diagnosticsBtn}
          onClick={() => setShowDrawer(true)}
          title={t.dataConfidence.diagnostics}
        >
          <Icon name="info" size="sm" />
        </button>
      </div>

      <DiagnosticsDrawer isOpen={showDrawer} onClose={() => setShowDrawer(false)} />
    </>
  );
}

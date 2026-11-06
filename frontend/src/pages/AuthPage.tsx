import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuthStore } from '../store/authStore';
import { useWalletStore } from '../store/walletStore';
import { useI18n } from '../i18n';
import { Icon } from '../components/Icon';
import { useIsMobile } from '../hooks/useMediaQuery';
import { LanguageToggle } from '../components/LanguageToggle';
import { ThemeToggle } from '../components/ThemeToggle';
import styles from './AuthPage.module.css';

export const AuthPage: React.FC = () => {
  const { t, locale } = useI18n();
  const navigate = useNavigate();
  const isMobile = useIsMobile();
  const { loginWithToken, isAuthenticated, isLoading, isInitialized, markAsInitialized } = useAuthStore();
  const { grantInitialFunds, hasReceivedInitialGrant } = useWalletStore();

  const [token, setToken] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [systemCheck, setSystemCheck] = useState({
    ws: 'pending',
    engine: 'pending',
    security: 'pending',
  });

  useEffect(() => {
    if (isAuthenticated) {
      // Grant initial funds if not already done
      if (!isInitialized && !hasReceivedInitialGrant) {
        const granted = grantInitialFunds();
        if (granted) {
          markAsInitialized();
        }
      }
      navigate('/trade');
    }
  }, [isAuthenticated, isInitialized, hasReceivedInitialGrant, grantInitialFunds, markAsInitialized, navigate]);

  // Simulate system checks on mount
  useEffect(() => {
    const runChecks = async () => {
      await new Promise(r => setTimeout(r, 600));
      setSystemCheck(prev => ({ ...prev, ws: 'ok' }));
      await new Promise(r => setTimeout(r, 400));
      setSystemCheck(prev => ({ ...prev, engine: 'ok' }));
      await new Promise(r => setTimeout(r, 500));
      setSystemCheck(prev => ({ ...prev, security: 'ok' }));
    };
    runChecks();
  }, []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    if (!token.trim()) {
      setError(t.auth?.tokenRequired || 'Access token is required');
      return;
    }

    const result = await loginWithToken(token.trim());
    if (!result.success) {
      setError(t.auth?.invalidToken || 'Invalid token. Check the ADMIN_TOKEN on the server.');
    }
  };

  return (
    <div className={styles.container}>
      <div className={styles.decor}>
        <div className={styles.grid} />
      </div>
      <div className={styles.overlay} />
      
      {/* Quick Settings Bar - Desktop Only */}
      {!isMobile && (
        <div className={styles.settingsBar}>
          <LanguageToggle />
          <div className={styles.settingsDivider} />
          <ThemeToggle />
        </div>
      )}

      <div className={styles.card}>
        <div className={styles.header}>
          <div className={styles.logo}>
            <Icon name="activity" size={isMobile ? "lg" : "xl"} strokeWidth={3} />
          </div>
          <span className={styles.title}>TBT TRADING</span>
          <p className={styles.subtitle}>{t.header.title}</p>
        </div>

        {/* Enhanced Welcome Banner */}
        <div className={styles.welcomeBanner}>
          <h3 className={styles.welcomeTitle}>
            {t.auth?.welcomeTitle || 'Welcome'}
          </h3>
          <div className={styles.welcomeContent}>
            <p className={styles.welcomeParagraph}>
              {isMobile 
                ? t.auth?.welcomeMessageMobile 
                : (locale === 'zh-CN' ? t.auth?.welcomeMessageZh : t.auth?.welcomeMessageEn)}
            </p>
          </div>
        </div>

        <form className={styles.form} onSubmit={handleSubmit}>
          <div className={styles.inputGroup}>
            <label className={styles.label}>{t.auth?.accessToken || 'Access Token'}</label>
            <div className={styles.inputWrapper}>
              <Icon name="key" size="sm" className={styles.inputIcon} />
              <input 
                type="password" 
                className={styles.input}
                value={token}
                onChange={(e) => setToken(e.target.value)}
                placeholder="Enter ADMIN_TOKEN"
                autoComplete="current-password"
                required
              />
            </div>
          </div>

          {error && (
            <div className={styles.errorMessage}>
              <Icon name="alert-circle" size="sm" />
              <span>{error}</span>
            </div>
          )}

          <button 
            type="submit" 
            className={styles.submitBtn} 
            disabled={isLoading}
          >
            {isLoading ? (
              <Icon name="loader" className={styles.spinner} />
            ) : (
              t.auth?.signIn || 'Login Now'
            )}
          </button>

          <div className={styles.modeSwitch}>
            <span className={styles.modeSwitchText}>
              {t.auth?.tokenHint || 'Single-user token access · stored locally in your browser'}
            </span>
          </div>
        </form>

        {/* Enhanced Footer - Desktop */}
        {!isMobile && (
          <div className={styles.footer}>
            <div className={styles.securityBadges}>
              <div className={styles.badge}>
                <Icon name="shield-check" size="xs" />
                <span>SSL SECURE</span>
              </div>
              <div className={styles.badge}>
                <Icon name="lock" size="xs" />
                <span>TOKEN AUTH</span>
              </div>
            </div>
            
            <div className={styles.systemStatus}>
              <div className={styles.statusHeader}>
                <Icon name="activity" size="xs" />
                <span>Real-Time Engine Status</span>
              </div>
              <div className={styles.statusGrid}>
                <div className={styles.statusItem}>
                  <span className={`${styles.dot} ${styles[systemCheck.ws]}`} />
                  <div className={styles.statusInfo}>
                    <span className={styles.statusLabel}>WebSocket</span>
                    <span className={styles.statusValue}>{systemCheck.ws === 'ok' ? 'Connected' : '...'}</span>
                  </div>
                </div>
                <div className={styles.statusItem}>
                  <span className={`${styles.dot} ${styles[systemCheck.engine]}`} />
                  <div className={styles.statusInfo}>
                    <span className={styles.statusLabel}>Matching</span>
                    <span className={styles.statusValue}>{systemCheck.engine === 'ok' ? 'Active' : '...'}</span>
                  </div>
                </div>
                <div className={styles.statusItem}>
                  <span className={`${styles.dot} ${styles[systemCheck.security]}`} />
                  <div className={styles.statusInfo}>
                    <span className={styles.statusLabel}>Security</span>
                    <span className={styles.statusValue}>{systemCheck.security === 'ok' ? 'Active' : '...'}</span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* Mobile Footer - Compact System Status & Security */}
        {isMobile && (
          <div className={styles.mobileFooter}>
            <div className={styles.mobileSystemStatus}>
              <div className={styles.mobileStatusItem}>
                <span className={`${styles.mobileStatusDot} ${systemCheck.ws === 'ok' ? styles.ok : systemCheck.ws === 'pending' ? styles.pending : ''}`} />
                <span>WS</span>
              </div>
              <div className={styles.mobileStatusItem}>
                <span className={`${styles.mobileStatusDot} ${systemCheck.engine === 'ok' ? styles.ok : systemCheck.engine === 'pending' ? styles.pending : ''}`} />
                <span>Engine</span>
              </div>
              <div className={styles.mobileStatusItem}>
                <span className={`${styles.mobileStatusDot} ${systemCheck.security === 'ok' ? styles.ok : systemCheck.security === 'pending' ? styles.pending : ''}`} />
                <span>Security</span>
              </div>
            </div>
            <div className={styles.mobileSecurityBadges}>
              <div className={styles.mobileSecurityBadge}>
                <Icon name="shield-check" size="xs" />
                <span>SSL</span>
              </div>
              <div className={styles.mobileSecurityBadge}>
                <Icon name="lock" size="xs" />
                <span>AUTH</span>
              </div>
            </div>
            {/* Mobile Language Toggle Only */}
            <div className={styles.mobileLanguageToggle}>
              <LanguageToggle />
            </div>
          </div>
        )}
      </div>
    </div>
  );
};
import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuthStore } from '../store/authStore';
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
  const { loginWithToken, isAuthenticated, isLoading } = useAuthStore();

  const [token, setToken] = useState('');
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (isAuthenticated) {
      // Account balances always come from the broker (/api/portfolio + WS
      // snapshots). The old local "initial grant" fabricated money and is gone.
      navigate('/trade');
    }
  }, [isAuthenticated, navigate]);

  // (2026-11-23 review P3) 登录页的 "Real-Time Engine Status" 状态灯是纯
  // 定时伪造的（注释自称 "Simulate system checks"），连同 systemCheck
  // state、样式与移动端同款指示灯一并删除——登录页不造假探测。

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
          </div>
        )}

        {/* Mobile Footer - Security & Language */}
        {isMobile && (
          <div className={styles.mobileFooter}>
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
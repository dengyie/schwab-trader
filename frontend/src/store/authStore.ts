import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import { hasToken, clearToken, verifyToken, probeSession, destroySession, getToken } from '../services/apiClient';

export interface User {
  id: string;
  username: string;
  email: string;
  displayName: string;
  avatar: string | null;
  bio: string;
  timezone: string;
  createdAt: number;
  lastLogin: number;
}

export interface UserPreferences {
  language: 'zh-CN' | 'en-US';
  theme: 'light' | 'dark' | 'system';
  quoteAsset: 'USD' | 'BTC';
  notifications: {
    priceThreshold: number;
    orderFills: boolean;
  };
}

interface AuthState {
  user: User | null;
  preferences: UserPreferences;
  isAuthenticated: boolean;
  isInitialized: boolean;
  isLoading: boolean;
  /** Persisted, non-sensitive: a session cookie was live at last check. */
  hadSession?: boolean;

  // Actions
  loginWithToken: (token: string) => Promise<{ success: boolean; error?: string }>;
  /** Boot: migrate a legacy stored token to a cookie, then probe the session. */
  establishSession: () => Promise<void>;
  logout: () => void;
  updateProfile: (updates: Partial<Pick<User, 'displayName' | 'bio' | 'timezone'>>) => void;
  updateAvatar: (dataUrl: string | null) => void;
  updatePreferences: (prefs: Partial<UserPreferences>) => void;
  markAsInitialized: () => void;
}

const defaultPreferences: UserPreferences = {
  language: 'zh-CN',
  theme: 'dark',
  quoteAsset: 'USD',
  notifications: {
    priceThreshold: 5,
    orderFills: true,
  },
};

// Placeholder display profile (2026-11-16 review): the old default carried
// a real personal email in every visitor's localStorage. The profile is
// editable in Settings; nothing here is a credential.
const defaultTrader: User = {
  id: 'trader-001',
  username: 'trader',
  email: 'trader@localhost',
  displayName: 'Trader',
  avatar: null,
  bio: 'Schwab & Alpaca Quant Trader',
  timezone: 'America/New_York',
  createdAt: Date.now(),
  lastLogin: Date.now(),
};

export const useAuthStore = create<AuthState>()(
  persist(
    (set, get) => ({
      user: defaultTrader,
      preferences: defaultPreferences,
      // Authenticated iff an admin token is stored — never a local-only flag.
      isAuthenticated: hasToken(),
      isInitialized: true,
      isLoading: false,

      loginWithToken: async (token: string) => {
        set({ isLoading: true });
        // 2026-11-16 token upgrade: verifyToken POSTs /auth/login, which
        // validates the admin token and sets an HttpOnly session cookie.
        // The token itself is used exactly once here and never stored —
        // localStorage no longer holds any admin credential.
        const ok = await verifyToken(token);
        if (!ok) {
          set({ isLoading: false });
          return { success: false, error: 'invalidToken' };
        }
        clearToken(); // drop any legacy stored token; the cookie owns auth now
        set({
          user: get().user || defaultTrader,
          isAuthenticated: true,
          isInitialized: true,
          isLoading: false,
        });
        // Rehydrate-time auto-start is auth-gated; a login after a logout
        // must re-arm the automation engine explicitly.
        void import('./automationStore').then(({ useAutomationStore }) => {
          useAutomationStore.getState().startEngine();
        });
        return { success: true };
      },

      logout: () => {
        // Revoke the server session (drops the HttpOnly cookie server-side
        // and on the browser) plus any legacy stored token.
        void destroySession();
        clearToken();
        // Wipe every piece of client-side risk state: an armed stop-loss
        // surviving into the next session on this browser would resume
        // firing orders for whoever logs in next (2026-11-16 review).
        import('./automationStore').then(({ useAutomationStore }) => {
          useAutomationStore.getState().resetForLogout();
        });
        // Clear wallet and trading data for privacy
        import('./walletStore').then(({ useWalletStore }) => {
          useWalletStore.getState().resetWallet();
        });
        import('./tradingStore').then(({ useTradingStore }) => {
          // Logging out only clears this browser; it must never reset the
          // broker account that other sessions may still be using.
          useTradingStore.getState().clearLocalState();
        });
        import('./notificationStore').then(({ useNotificationStore }) => {
          useNotificationStore.getState().clear();
        });

        set({
          user: null,
          isAuthenticated: false,
          isInitialized: false,
          // Keep preferences for next login
        });
      },

      /** Boot-time session establishment.
       *
       * A legacy stored admin token is exchanged ONCE for a cookie and then
       * erased from localStorage; every boot afterwards probes the cookie.
       * A 401 here flows through apiFetch's unauthorized event, which
       * routes the app to the login gate. */
      establishSession: async () => {
        if (hasToken()) {
          const legacy = getToken();
          if (legacy && await verifyToken(legacy)) {
            clearToken();
            set({ isAuthenticated: true, isInitialized: true });
          } else {
            clearToken();
          }
        }
        if (get().isAuthenticated) {
          const live = await probeSession();
          if (!live) {
            // probeSession's 401 already fired the unauthorized event.
            set({ isAuthenticated: false });
          }
        }
      },

      updateProfile: (updates) => {
        set((state) => {
          if (!state.user) return state;
          return {
            user: {
              ...state.user,
              ...updates,
            },
          };
        });
      },

      updateAvatar: (dataUrl) => {
        set((state) => {
          if (!state.user) return state;
          return {
            user: {
              ...state.user,
              avatar: dataUrl,
            },
          };
        });
      },

      updatePreferences: (prefs) => {
        set((state) => ({
          preferences: {
            ...state.preferences,
            ...prefs,
            notifications: {
              ...state.preferences.notifications,
              ...(prefs.notifications || {}),
            },
          },
        }));
      },

      markAsInitialized: () => {
        set({ isInitialized: true });
      },
    }),
    {
      name: 'paper-auth-storage',
      version: 3,
      partialize: (state) => ({
        user: state.user,
        preferences: state.preferences,
        isInitialized: state.isInitialized,
        // Non-sensitive marker: a cookie session probably exists. The boot
        // probe (establishSession) is the authority — this only keeps the
        // login gate from flashing before the probe answers.
        hadSession: state.isAuthenticated,
      }),
      // Merge: optimistic session assumption from the non-sensitive marker;
      // never from a stored credential (none is stored anymore).
      merge: (persisted, current) => ({
        ...current,
        ...(persisted as Partial<AuthState>),
        isAuthenticated: hasToken() || Boolean((persisted as Partial<AuthState> | undefined)?.hadSession),
      }),
    }
  )
);

// Selectors
export const selectUser = (state: AuthState) => state.user;
export const selectPreferences = (state: AuthState) => state.preferences;
export const selectIsAuthenticated = (state: AuthState) => state.isAuthenticated;
export const selectIsInitialized = (state: AuthState) => state.isInitialized;

export function currentToken(): string | null {
  return getToken();
}
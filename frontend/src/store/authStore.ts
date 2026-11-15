import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import { hasToken, setToken, clearToken, verifyToken, getToken } from '../services/apiClient';

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

  // Actions
  loginWithToken: (token: string) => Promise<{ success: boolean; error?: string }>;
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

const defaultTrader: User = {
  id: 'trader-001',
  username: 'mango',
  email: 'dengyie@gmail.com',
  displayName: 'Mango Pro Trader',
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
        const ok = await verifyToken(token);
        if (!ok) {
          set({ isLoading: false });
          return { success: false, error: 'invalidToken' };
        }
        setToken(token);
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
      }),
      // Merge: local persistence never asserts authentication; the token decides.
      merge: (persisted, current) => ({
        ...current,
        ...(persisted as Partial<AuthState>),
        isAuthenticated: hasToken(),
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
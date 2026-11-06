import type { Config } from 'tailwindcss';

export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        cyber: {
          950: '#06080e',
          900: '#0b0f19',
          850: '#0f1422',
          800: '#151b2d',
          700: '#1e263d',
          600: '#2d3752',
        },
        trade: {
          up: '#10b981',
          'up-glow': 'rgba(16, 185, 129, 0.25)',
          down: '#f43f5e',
          'down-glow': 'rgba(244, 63, 94, 0.25)',
          cyan: '#06b6d4',
          gold: '#f59e0b',
          purple: '#8b5cf6',
        },
      },
      fontFamily: {
        sans: ['Inter', '-apple-system', 'BlinkMacSystemFont', 'Segoe UI', 'Roboto', 'sans-serif'],
        mono: ['JetBrains Mono', 'SFMono-Regular', 'Menlo', 'Monaco', 'Consolas', 'monospace'],
      },
      boxShadow: {
        'glass-rim': 'inset 0 1px 1px 0 rgba(255, 255, 255, 0.12), inset 0 -1px 1px 0 rgba(0, 0, 0, 0.4)',
        'glass-rim-glow': 'inset 0 1px 1px 0 rgba(255, 255, 255, 0.2), 0 8px 32px 0 rgba(0, 0, 0, 0.37)',
      },
      animation: {
        'pulse-subtle': 'pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        'fade-in': 'fadeIn 0.25s cubic-bezier(0.16, 1, 0.3, 1)',
      },
      keyframes: {
        fadeIn: {
          '0%': { opacity: '0', transform: 'translateY(4px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
      },
    },
  },
  plugins: [],
} satisfies Config;

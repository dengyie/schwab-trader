import React, { useEffect } from 'react';
import { CheckCircle2, AlertCircle, Info, XCircle, X } from 'lucide-react';

export interface ToastItem {
  id: string;
  title: string;
  message?: string;
  type: 'success' | 'error' | 'info' | 'warning';
}

interface ToastStackProps {
  toasts: ToastItem[];
  onDismiss: (id: string) => void;
}

export const ToastStack: React.FC<ToastStackProps> = ({ toasts, onDismiss }) => {
  return (
    <div className="fixed bottom-16 right-5 z-50 flex flex-col gap-2 pointer-events-none max-w-sm w-full">
      {toasts.map(toast => (
        <ToastCard key={toast.id} toast={toast} onDismiss={onDismiss} />
      ))}
    </div>
  );
};

const ToastCard: React.FC<{ toast: ToastItem; onDismiss: (id: string) => void }> = ({ toast, onDismiss }) => {
  useEffect(() => {
    const timer = setTimeout(() => {
      onDismiss(toast.id);
    }, 4500);
    return () => clearTimeout(timer);
  }, [toast.id, onDismiss]);

  const icons = {
    success: <CheckCircle2 className="w-4 h-4 text-emerald-400 shrink-0" />,
    error: <XCircle className="w-4 h-4 text-rose-400 shrink-0" />,
    warning: <AlertCircle className="w-4 h-4 text-amber-400 shrink-0" />,
    info: <Info className="w-4 h-4 text-cyan-400 shrink-0" />
  };

  const borderColors = {
    success: 'border-emerald-500/30 bg-[#0d1720]/90 text-emerald-200',
    error: 'border-rose-500/30 bg-[#1c0d12]/90 text-rose-200',
    warning: 'border-amber-500/30 bg-[#1a140d]/90 text-amber-200',
    info: 'border-cyan-500/30 bg-[#0b1422]/90 text-cyan-200'
  };

  return (
    <div
      className={`pointer-events-auto p-3.5 rounded-xl border backdrop-blur-xl shadow-2xl flex items-start gap-3 transition-all animate-fade-in ${borderColors[toast.type]}`}
    >
      <div className="mt-0.5">{icons[toast.type]}</div>
      <div className="flex-1 min-w-0">
        <div className="font-semibold text-xs text-slate-100">{toast.title}</div>
        {toast.message && (
          <div className="text-[11px] text-slate-300 mt-0.5 font-mono leading-relaxed">
            {toast.message}
          </div>
        )}
      </div>
      <button
        type="button"
        onClick={() => onDismiss(toast.id)}
        className="p-1 rounded text-slate-400 hover:text-white transition-colors"
      >
        <X className="w-3.5 h-3.5" />
      </button>
    </div>
  );
};

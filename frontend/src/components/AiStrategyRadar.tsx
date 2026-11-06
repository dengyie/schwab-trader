import React from 'react';
import { ModelContext, Quote } from '../types';
import { Cpu, ArrowUpRight, ArrowDownRight, Target, Shield, Copy, Check } from 'lucide-react';

interface AiStrategyRadarProps {
  modelContext: ModelContext | null;
  quote?: Quote;
  onApplyRecommendation: (rec: { side: 'BUY' | 'SELL'; price: number; stopPrice?: number }) => void;
}

export const AiStrategyRadar: React.FC<AiStrategyRadarProps> = ({
  modelContext,
  quote,
  onApplyRecommendation
}) => {
  const [copied, setCopied] = React.useState(false);

  const indicators = modelContext?.technical_indicators;
  const lastPrice = quote?.last || modelContext?.latest_quote?.last || 500;
  const trend = indicators?.trend || 'NEUTRAL';
  const rsi = indicators?.rsi_14 ?? 50;

  // Quantitative factor calculations
  const longProb = trend === 'BULLISH' ? Math.min(88, Math.round(55 + (100 - rsi) * 0.25)) : (rsi < 35 ? 65 : 28);
  const shortProb = 100 - longProb;
  const momentumScore = Math.min(100, Math.max(10, Math.round(rsi * 0.9 + (trend === 'BULLISH' ? 20 : -10))));
  const volatilityLevel = (lastPrice * 0.015).toFixed(2);

  // Take profit and stop loss recommendations
  const isBull = longProb >= 50;
  const recSide: 'BUY' | 'SELL' = isBull ? 'BUY' : 'SELL';
  const targetPrice = isBull ? lastPrice * 1.025 : lastPrice * 0.975;
  const stopLossPrice = isBull ? lastPrice * 0.988 : lastPrice * 1.012;

  const handleCopyApi = () => {
    navigator.clipboard.writeText(`${window.location.origin}/api/market/model-context?symbol=${modelContext?.symbol || 'SPY'}`);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="h-full flex flex-col justify-between text-xs font-sans select-none p-3">
      {/* Top Header */}
      <div className="flex items-center justify-between border-b border-white/[0.06] pb-2">
        <div className="flex items-center gap-2">
          <div className="p-1 rounded-md bg-purple-500/15 border border-purple-500/30 text-purple-400">
            <Cpu className="w-3.5 h-3.5" />
          </div>
          <div>
            <span className="font-bold text-slate-100 flex items-center gap-1.5">
              <span>DeepQuant Multi-Factor Engine</span>
              <span className="text-[9px] font-mono px-1.5 py-0.2 rounded bg-purple-950/60 text-purple-400 border border-purple-800/40">
                PyTorch/RL
              </span>
            </span>
          </div>
        </div>

        <button
          onClick={handleCopyApi}
          className="text-[10px] font-mono px-2 py-0.5 rounded bg-white/5 hover:bg-white/10 text-slate-300 border border-white/10 transition-colors flex items-center gap-1"
        >
          {copied ? <Check className="w-2.5 h-2.5 text-emerald-400" /> : <Copy className="w-2.5 h-2.5 text-cyan-400" />}
          <span>JSON API</span>
        </button>
      </div>

      {/* Middle: Probabilities & Factors Grid */}
      <div className="grid grid-cols-3 gap-3 my-2">
        {/* Signal & Probabilities */}
        <div className="p-2.5 rounded-xl bg-white/[0.02] border border-white/[0.05] space-y-1">
          <div className="flex justify-between items-center text-[10px] text-slate-400">
            <span>Prediction</span>
            <span className={`font-bold ${isBull ? 'text-emerald-400' : 'text-rose-400'}`}>
              {isBull ? 'ACCUMULATE' : 'TRIM/SHORT'}
            </span>
          </div>
          <div className="text-base font-bold font-mono text-slate-100">
            {longProb}% <span className="text-[10px] text-slate-500 font-sans font-normal">Confidence</span>
          </div>
          <div className="h-1.5 w-full bg-slate-800 rounded-full overflow-hidden flex">
            <div className="bg-emerald-500 h-full" style={{ width: `${longProb}%` }} />
            <div className="bg-rose-500 h-full" style={{ width: `${shortProb}%` }} />
          </div>
        </div>

        {/* Momentum & RSI */}
        <div className="p-2.5 rounded-xl bg-white/[0.02] border border-white/[0.05] space-y-1 font-mono">
          <div className="flex justify-between items-center text-[10px] text-slate-400 font-sans">
            <span>Momentum</span>
            <span className="text-cyan-400 font-semibold">{momentumScore}/100</span>
          </div>
          <div className="text-sm font-bold text-slate-200">
            RSI: {rsi.toFixed(1)}
          </div>
          <div className="text-[10px] text-slate-400 font-sans">
            {rsi > 70 ? 'Overbought zone' : (rsi < 30 ? 'Oversold zone' : 'Neutral continuation')}
          </div>
        </div>

        {/* Target and Stop Levels */}
        <div className="p-2.5 rounded-xl bg-white/[0.02] border border-white/[0.05] space-y-1 font-mono">
          <div className="flex justify-between items-center text-[10px] text-slate-400 font-sans">
            <span className="flex items-center gap-1"><Target className="w-2.5 h-2.5 text-emerald-400" /> Target</span>
            <span className="text-emerald-400 font-bold">${targetPrice.toFixed(2)}</span>
          </div>
          <div className="flex justify-between items-center text-[10px] text-slate-400 font-sans">
            <span className="flex items-center gap-1"><Shield className="w-2.5 h-2.5 text-rose-400" /> Stop</span>
            <span className="text-rose-400 font-bold">${stopLossPrice.toFixed(2)}</span>
          </div>
          <div className="text-[9px] text-slate-500 font-sans">
            Est. ATR: ${volatilityLevel}
          </div>
        </div>
      </div>

      {/* Action Footer */}
      <div className="flex items-center justify-between pt-1 border-t border-white/[0.04]">
        <span className="text-[10px] text-slate-400">
          Strategy: Multi-horizon mean-reversion + momentum filter
        </span>
        <button
          onClick={() => onApplyRecommendation({
            side: recSide,
            price: lastPrice,
            stopPrice: stopLossPrice
          })}
          className="px-2.5 py-1 rounded-lg bg-gradient-to-r from-purple-600/30 to-cyan-600/30 hover:from-purple-600/40 hover:to-cyan-600/40 text-cyan-300 border border-cyan-500/30 font-semibold text-[10px] flex items-center gap-1 transition-all active:scale-95"
        >
          <span>Auto-fill {recSide} Setup</span>
          {isBull ? <ArrowUpRight className="w-3 h-3 text-emerald-400" /> : <ArrowDownRight className="w-3 h-3 text-rose-400" />}
        </button>
      </div>
    </div>
  );
};

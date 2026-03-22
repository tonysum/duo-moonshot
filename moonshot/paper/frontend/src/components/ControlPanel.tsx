import React from 'react';
import { triggerScan, stopRunner, startRunner } from '../api';

interface ControlPanelProps {
  isRunning: boolean;
  onRefresh: () => void;
  isDual?: boolean;
  activeStrategy?: 'daily' | 'rolling';
  onStrategyChange?: (s: 'daily' | 'rolling') => void;
}

export const ControlPanel: React.FC<ControlPanelProps> = ({ isRunning, onRefresh, isDual, activeStrategy }) => {
  const handleScan = async () => {
    const msg = isDual
      ? `Trigger scan for ${activeStrategy === 'rolling' ? 'Rolling' : 'Daily'}?`
      : "Trigger manual scan?";
    if (confirm(msg)) {
      await triggerScan(isDual ? activeStrategy : undefined);
      alert("Scan triggered!");
      onRefresh();
    }
  };

  const handleScanBoth = async () => {
    if (!isDual) return;
    if (confirm("Trigger Daily + Rolling scans?")) {
      await triggerScan();
      alert("Both scans scheduled!");
      onRefresh();
    }
  };

  const handleToggle = async () => {
    if (isRunning) {
      if (confirm("Stop the paper runner?")) {
        await stopRunner();
        onRefresh();
      }
    } else {
      if (confirm("Start the paper runner?")) {
        await startRunner();
        onRefresh();
      }
    }
  };

  return (
    <div className="brut-border brut-shadow bg-accent p-6 flex flex-col gap-4 text-white">
      <h2 className="text-2xl font-black uppercase">Admin Controls</h2>
      <div className="flex flex-col gap-3">
        <div className="flex gap-4">
          <button
            onClick={handleScan}
            className="flex-1 brut-border brut-shadow-hover bg-white text-black font-black uppercase py-4 text-xl"
          >
            {isDual ? `🚀 Scan (${activeStrategy})` : "🚀 Manual Scan"}
          </button>
          <button
            onClick={handleToggle}
            className={`flex-1 brut-border brut-shadow-hover font-black uppercase py-4 text-xl ${
              isRunning ? 'bg-red-600 text-white' : 'bg-green-500 text-black'
            }`}
          >
            {isRunning ? '🛑 Stop Runner' : '▶️ Start Runner'}
          </button>
        </div>
        {isDual && (
          <button
            onClick={handleScanBoth}
            className="w-full brut-border brut-shadow-hover bg-yellow-300 text-black font-black uppercase py-3 text-sm"
          >
            🔄 Scan Daily + Rolling
          </button>
        )}
      </div>
    </div>
  );
};

import React from 'react';
import { triggerScan, stopRunner, startRunner } from '../api';

interface ControlPanelProps {
  isRunning: boolean;
  onRefresh: () => void;
}

export const ControlPanel: React.FC<ControlPanelProps> = ({ isRunning, onRefresh }) => {
  const handleScan = async () => {
    if (confirm("Trigger manual scan?")) {
      await triggerScan();
      alert("Scan triggered!");
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
      <div className="flex gap-4">
        <button
          onClick={handleScan}
          className="flex-1 brut-border brut-shadow-hover bg-white text-black font-black uppercase py-4 text-xl"
        >
          🚀 Manual Scan
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
    </div>
  );
};

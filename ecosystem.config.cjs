// PM2 Ecosystem — Moonshot Paper Trading
// Usage: pm2 start ecosystem.config.cjs

module.exports = {
  apps: [
    {
      name: "moonshot-paper",
      script: ".venv/bin/python",
      args: "-m moonshot.paper start --port 8100",
      cwd: __dirname,
      interpreter: "none",  // use the script directly
      env: {
        PYTHONUNBUFFERED: "1",
      },
      // Auto-restart
      autorestart: true,
      max_restarts: 10,
      restart_delay: 5000,
      // Logs
      log_date_format: "YYYY-MM-DD HH:mm:ss",
      error_file: "logs/paper-error.log",
      out_file: "logs/paper-out.log",
      merge_logs: true,
      // Resource
      max_memory_restart: "500M",
    },
  ],
};

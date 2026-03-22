// PM2 Ecosystem — Moonshot Paper Trading（云服务器）
//
// 密钥：项目根目录 .env 中 BINANCE_* 即可（paper 入口会 load_dotenv）；
// 也可在下方 env 填写或 shell export，后两者会覆盖 .env 同名变量。
//
// 策略 / 端口：改下方 env 里的 PAPER_STRATEGY、PAPER_PORT，然后
//   pm2 restart moonshot-paper --update-env
//
// PAPER_STRATEGY: daily | rolling | both

const path = require("path");

module.exports = {
  apps: [
    {
      name: "moonshot-paper",
      script: path.join(__dirname, "scripts", "paper-pm2.sh"),
      cwd: __dirname,
      interpreter: "bash",
      env: {
        PYTHONUNBUFFERED: "1",
        PAPER_PORT: "8100",
        PAPER_STRATEGY: "both",
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

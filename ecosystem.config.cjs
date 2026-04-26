// PM2 Ecosystem — Moonshot Paper（raw-surge 单进程，无 daily/rolling 切换）
//
// 密钥：项目根目录 .env 中 BINANCE_*；可选 MOONSHOT_R24_RAW_SURGE_PARAMS 指向参数 JSON
//
// 端口：改 PAPER_PORT；显式参数文件：设 PAPER_CONFIG 为相对项目根的路径（会传给 `paper start --config`）。
// 不设 PAPER_CONFIG 时与本地 `python -m moonshot.paper start` 相同（环境变量 + 自动发现 config/r24_raw_surge_params.json）
// 改 env 后：pm2 restart moonshot-paper --update-env

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
        // 若固定使用仓库内文件，可取消下一行注释：
        // PAPER_CONFIG: "config/r24_raw_surge_params.json",
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

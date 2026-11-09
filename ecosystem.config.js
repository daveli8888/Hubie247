// pm2 start ecosystem.config.js   (pm2 restarts any part that exits; the watchdog restarts parts that hang)
const common = {
  cwd: __dirname,
  interpreter: 'none',
  autorestart: true,
  restart_delay: 2000,
  exp_backoff_restart_delay: 1000,
  max_restarts: 1000,
  min_uptime: '10s',
  kill_timeout: 8000,
  time: true,
};
const app = (name, mod, extra = {}) => ({ ...common, name: `hubie247-${name}`, script: process.env.HUBIE_PYTHON || 'python3', args: `-u -m hubie247.${mod}`, ...extra });
module.exports = {
  apps: [
    // stage + its ffmpeg get CPU priority (HUBIE_STAGE_NICE, default -10) so a busy Chrome can't stall the encoder
    { ...app('stage', 'stage'), script: 'nice',
      args: `-n ${process.env.HUBIE_STAGE_NICE || '-10'} ${process.env.HUBIE_PYTHON || 'python3'} -u -m hubie247.stage` },
    app('game', 'game', { max_memory_restart: '700M' }),
    app('brain', 'brain'),
    app('watchdog', 'watchdog'),
    app('kickhook', 'kick_webhook'),
  ],
};

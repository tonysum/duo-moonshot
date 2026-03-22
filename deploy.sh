#!/bin/bash
# Moonshot Paper Trading — Deploy Script
# Usage: bash deploy.sh

set -e
echo "🚀 Moonshot Paper Trading — Deploy"

# 1. Install Python dependencies
echo "📦 Installing Python dependencies..."
uv sync

# 2. Build frontend
echo "🔨 Building frontend..."
cd moonshot/paper/frontend
npm install
npm run build
cd ../../..

# 3. Create logs directory
mkdir -p logs

# 4. Start with PM2
echo "🟢 Starting with PM2..."
pm2 start ecosystem.config.cjs
pm2 save

echo ""
echo "✅ Done! Access: http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo 'localhost'):8100"
echo ""
echo "📋 Useful commands:"
echo "   pm2 status          — check status"
echo "   pm2 logs moonshot-paper  — view logs"
echo "   pm2 restart moonshot-paper --update-env — restart (after env changes)"
echo "   pm2 stop moonshot-paper    — stop"

#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/home/pi/hybrid-portfolio"
DEPLOY_DIR="$PROJECT_DIR/deploy"

cd "$PROJECT_DIR"

python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt

sudo cp "$DEPLOY_DIR/haa-rebalance.service" /etc/systemd/system/
sudo cp "$DEPLOY_DIR/haa-rebalance.timer" /etc/systemd/system/
sudo cp "$DEPLOY_DIR/olweather-rebalance.service" /etc/systemd/system/
sudo cp "$DEPLOY_DIR/olweather-rebalance.timer" /etc/systemd/system/
sudo cp "$DEPLOY_DIR/sleeve-monitor.service" /etc/systemd/system/
sudo cp "$DEPLOY_DIR/sleeve-monitor.timer" /etc/systemd/system/
sudo cp "$DEPLOY_DIR/coin-rebalance.service" /etc/systemd/system/
sudo cp "$DEPLOY_DIR/coin-rebalance.timer" /etc/systemd/system/
sudo cp "$DEPLOY_DIR/hybrid-bot.service" /etc/systemd/system/
sudo cp "$DEPLOY_DIR/notify-failure@.service" /etc/systemd/system/
sudo cp "$DEPLOY_DIR/heartbeat.service" /etc/systemd/system/
sudo cp "$DEPLOY_DIR/heartbeat.timer" /etc/systemd/system/
sudo cp "$DEPLOY_DIR/performance.service" /etc/systemd/system/
sudo cp "$DEPLOY_DIR/performance.timer" /etc/systemd/system/
sudo cp "$DEPLOY_DIR/performance-us.service" /etc/systemd/system/
sudo cp "$DEPLOY_DIR/performance-us.timer" /etc/systemd/system/
sudo cp "$DEPLOY_DIR/coin-exp-buy.service" /etc/systemd/system/
sudo cp "$DEPLOY_DIR/coin-exp-buy.timer" /etc/systemd/system/
sudo cp "$DEPLOY_DIR/coin-exp-sell.service" /etc/systemd/system/
sudo cp "$DEPLOY_DIR/coin-exp-sell.timer" /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable --now haa-rebalance.timer
sudo systemctl enable --now olweather-rebalance.timer
sudo systemctl enable --now sleeve-monitor.timer
sudo systemctl enable --now coin-rebalance.timer
sudo systemctl enable --now heartbeat.timer
sudo systemctl enable --now performance.timer
sudo systemctl enable --now performance-us.timer
sudo systemctl enable --now coin-exp-buy.timer
sudo systemctl enable --now coin-exp-sell.timer
sudo systemctl enable --now hybrid-bot.service

echo "설치 완료"
echo "  타이머 상태: systemctl status haa-rebalance.timer olweather-rebalance.timer sleeve-monitor.timer coin-rebalance.timer"
echo "  봇 상태:     systemctl status hybrid-bot.service"

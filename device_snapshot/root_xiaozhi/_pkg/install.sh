#!/bin/sh
# 小智AI 一键安装（MaixCAM2 设备上执行）
set -e
echo "=== 1. 目录 ==="
mkdir -p /root/xiaozhi/icons /root/uishim /maixapp/apps/xiaozhi

echo "=== 2. 编译注入库 ==="
cd /root/uishim
gcc -O2 -fPIC -shared -o libmaixcam2_overlay.so maixcam2_overlay.c -ldl -lpthread
echo "compiled: $(ls -la libmaixcam2_overlay.so)"

echo "=== 3. systemd drop-in（状态栏注入） ==="
mkdir -p /etc/systemd/system/launcher.service.d
cat > /etc/systemd/system/launcher.service.d/zz-overlay.conf << 'EOC'
[Service]
Environment=LD_PRELOAD=/root/uishim/libmaixcam2_overlay.so
Environment=MAIXCAM2_GF=1
Environment=MAIXCAM2_GF_LOG=/tmp/statusbar.log
Environment=MAIXCAM2_OVL=/root/xiaozhi/status.rgba:10:5:0,/root/xiaozhi/popup.rgba:432:68:0
EOC
mkdir -p /etc/systemd/system/maixvision-server.service.d
cp /etc/systemd/system/launcher.service.d/zz-overlay.conf \
   /etc/systemd/system/maixvision-server.service.d/zz-overlay.conf

echo "=== 4. 后台服务 ==="
cp /root/xiaozhi/_pkg/xiaozhi.service /etc/systemd/system/xiaozhi.service
systemctl daemon-reload
systemctl enable xiaozhi.service

echo "=== 5. App 安装（启动器图标） ==="
cp -f /root/xiaozhi/_pkg/app.yaml /maixapp/apps/xiaozhi/
cp -f /root/xiaozhi/_pkg/main.py /maixapp/apps/xiaozhi/
cp -f /root/xiaozhi/_pkg/icon.png /maixapp/apps/xiaozhi/
chmod -R a+rX /maixapp/apps/xiaozhi
python3 /maixapp/apps/gen_app_info.py /maixapp/apps

echo "=== 6. 启动 ==="
systemctl restart xiaozhi.service || true
systemctl restart launcher.service || true

echo "=== 完成 ==="
echo "查看服务: systemctl status xiaozhi"
echo "日志: journalctl -u xiaozhi -f"
echo "临时关闭注入: touch /root/uishim/DISABLE && systemctl restart launcher"

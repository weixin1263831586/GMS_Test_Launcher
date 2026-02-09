#!/bin/bash
set -e

if [[ $# -lt 2 ]]; then
    echo "Usage: $0 <SerialNo> <lock|unlock>"
    exit 1
fi

SERIAL="$1"
ACTION="$2"

echo "🔄 重启设备 $SERIAL 进入 bootloader..."
adb -s "$SERIAL" reboot bootloader
sleep 5

echo "🔐 执行 $ACTION 操作..."
fastboot -s "$SERIAL" oem at-"$ACTION"-vboot
fastboot -s "$SERIAL" reboot fastboot
sleep 3

echo "🔄 重启设备..."
fastboot -s "$SERIAL" reboot

echo "✅ 设备 $SERIAL $ACTION 完成!"

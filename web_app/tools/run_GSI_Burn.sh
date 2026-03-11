#!/bin/bash
set -euo pipefail

DEVICE=""
SYSTEM_IMG=""
VENDOR_IMG=""

# 解析命令行参数
while [[ $# -gt 0 ]]; do
    case "$1" in
        --system)
            shift
            SYSTEM_IMG="$1"
            ;;
        --vendor)
            shift
            VENDOR_IMG="$1"
            ;;
        *)
            if [[ -z "$DEVICE" ]]; then
                DEVICE="$1"
            else
                echo "未知参数: $1" >&2
                exit 1
            fi
            ;;
    esac
    shift
done

if [[ -z "$DEVICE" ]] || [[ -z "$SYSTEM_IMG" ]]; then
    echo "Usage: $0 <device> --system <system.img> [--vendor <vendor_boot.img>]" >&2
    exit 1
fi

if [[ ! -f "$SYSTEM_IMG" ]]; then
    echo "❌ System 镜像不存在: $SYSTEM_IMG" >&2
    exit 1
fi

echo "🔄 重启设备 $DEVICE 进入 bootloader..."
adb -s "$DEVICE" reboot bootloader
sleep 5

echo "🔓 解锁 vboot..."
fastboot -s "$DEVICE" oem at-unlock-vboot
fastboot -s "$DEVICE" reboot fastboot
sleep 3

echo "🗑️ 删除 product 分区..."
fastboot -s "$DEVICE" delete-logical-partition product
fastboot -s "$DEVICE" delete-logical-partition product_a
fastboot -s "$DEVICE" delete-logical-partition product_b

echo "💾 烧写 system 镜像..."
fastboot -s "$DEVICE" flash system "$SYSTEM_IMG"

fastboot -s "$DEVICE" flash misc /home/hcq/GMS-Suite/misc.img

if [[ -n "$VENDOR_IMG" ]]; then
    if [[ -f "$VENDOR_IMG" ]]; then
        echo "💾 烧写 vendor_boot 镜像..."
        fastboot -s "$DEVICE" flash vendor_boot "$VENDOR_IMG"
    else
        echo "⚠️ Vendor boot 镜像不存在，跳过: $VENDOR_IMG"
    fi
fi

echo "🔄 重启设备..."
fastboot -s "$DEVICE" reboot

echo "✅ GSI 烧写完成!"
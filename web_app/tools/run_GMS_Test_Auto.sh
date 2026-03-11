#!/bin/bash
set -euo pipefail

export PATH="$HOME/Software/sdk_tools_new/sdk_tools:$PATH"

LOG_FILE="/tmp/gms_test_$(date +%Y%m%d_%H%M%S).log"

# 运行状态
REMOTE_HOST=""
REMOTE_USER=""
SUITE_PATH=""
SUITE_PREFIX=""
TEST_COMMAND=""
DEVICE_ARGS=""
MODE="run"
PASS_COUNT=0
FAIL_COUNT=0
RESULT_TIMESTAMP=""
RETRY_FAIL="true"
COPY_TO_REMOTE="false"

# 工具函数
log() { echo -e "$*" | tee -a "$LOG_FILE"; }
die() { log "❌ $*"; exit 1; }

show_help() {
cat <<EOF
用法:
  $0 <cts|gsi|gts|sts|vts|apts> [模块] [用例]
  $0 <cts|gsi|gts|sts|vts|apts> retry <RESULT_TIMESTAMP>

必需参数:
  --test-suite path         测试套件完整路径(如：/home/user/GMS-Suite/android-cts-16_r3-1/android-cts/tools)
  --local-server user@host  本地主机

可选参数:
  --device-args ARGS        设备参数, 格式：[-s DEVICE1] 或 [--shard-count 2 -s DEVICE1 -s DEVICE2...]
  --no-retry                禁用失败自动重试
  --copy-remote             测试结果拷贝到远端 
  --help                    显示帮助

示例:
  $0 cts CtsSecurityTestCases --device-args '-s RK3576GMS1' --test-suite /home/hcq/GMS-Suite/android-cts-16_r3-1/android-cts/tools --local-server hcq@10.10.10.206
  $0 cts retry 2026.01.12_14.36.17.772_8696 --device-args '-s RK3576GMS1' --test-suite /home/hcq/GMS-Suite/android-cts-16_r3-1/android-cts/tools --local-server hcq@10.10.10.206

支持测试类型: cts, gsi, gts, sts, vts, apts
EOF
}

## 参数解析
parse_args() {
    local args=()
    DEVICE_ARGS=""
    log "🔧 开始解析命令行参数 ($# 个)"
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --help)
                show_help
                exit 0
                ;;
            --no-retry)
                RETRY_FAIL="false"
                log "✅ 禁用自动重试"
                shift
                ;;
            --local-server)
                shift
                if [[ $# -eq 0 ]]; then
                    die "--local-server 缺少参数（格式: user@host）"
                fi
                local_server="$1"
                if [[ "$local_server" != *@* ]]; then
                    die "--local-server 格式错误，应为 user@host"
                fi
                REMOTE_USER="${local_server%@*}"
                REMOTE_HOST="${local_server#*@}"
                log "📁 本地主机: ${REMOTE_USER}@${REMOTE_HOST}"
                shift
                ;;
            --test-suite)
                shift
                if [[ $# -eq 0 ]]; then
                    die "--test-suite 缺少路径参数"
                fi
                SUITE_PATH="$1"
                log "📁 测试套件路径: $SUITE_PATH"
                shift
                ;;
            --device-args)
                shift
                log "📌 处理 --device-args 参数..."
                if [[ $# -gt 0 ]]; then
                    DEVICE_ARGS="$1"
                    shift
                else
                    die "--device-args 缺少参数"
                fi
                while [[ $# -gt 0 ]] && [[ ! "$1" =~ ^-- ]]; do
                    DEVICE_ARGS+=" $1"
                    shift
                done
                if [[ -z "$DEVICE_ARGS" ]]; then
                    die "缺少设备参数，请使用 --device-args 指定设备"
                fi
                if [[ ! "$DEVICE_ARGS" =~ -s[[:space:]]+[^[:space:]]+ ]]; then
                    die "⚠️ 设备参数格式可能不正确，应为: -s DEVICE1 [-s DEVICE2 ...]"
                fi
                log "📱 设备参数: '$DEVICE_ARGS'"
                ;;
            --copy-remote)
                COPY_TO_REMOTE="true"
                log "✅ 启用结果拷贝到远程"
                shift
                ;;
            -*)
                die "未知参数: $1"
                ;;
            *)
                args+=("$1")
                shift
                ;;
        esac
    done

    if (( ${#args[@]} < 1 )); then
        die "缺少测试类型"
    fi
    if [[ -z "$SUITE_PATH" ]]; then
        die "缺少必需参数: --test-suite"
    fi
    if [[ -z "$REMOTE_HOST" ]] || [[ -z "$REMOTE_USER" ]]; then
        die "缺少必需参数: --local-server"
    fi

    Test_Type="${args[0],,}"
    Test_Module="${args[1]:-}"
    Test_Case="${args[2]:-}"

    if [[ "${Test_Module,,}" == "retry" ]]; then
        MODE="retry"
        RESULT_TIMESTAMP="$Test_Case"
        if [[ -z "$RESULT_TIMESTAMP" ]]; then
            die "retry 必须指定 RESULT_TIMESTAMP"
        fi
        Test_Module=""; Test_Case=""
        log "🔄 Retry 模式: $RESULT_TIMESTAMP"
    else
        MODE="run"
        log "🧪 测试配置: 类型=$Test_Type, 模块=$Test_Module, 用例=$Test_Case"
    fi

    case "${Test_Type}" in
        cts)
            SUITE_PREFIX="cts"
            TEST_COMMAND="cts"
            ;;
        gsi)
            SUITE_PREFIX="cts"
            TEST_COMMAND="cts-on-gsi"
            ;;
        gts)
            SUITE_PREFIX="gts"
            TEST_COMMAND="gts"
            ;;
        sts)
            SUITE_PREFIX="sts"
            TEST_COMMAND="sts-dynamic-full"
            ;;
        vts)
            SUITE_PREFIX="vts"
            TEST_COMMAND="vts"
            ;;
        apts)
            SUITE_PREFIX="gts"
            TEST_COMMAND="apts"
            ;;
        *)
            die "不支持的测试类型: $Test_Type (目前仅支持: cts, gsi, gts, sts, vts, apts)"
            ;;
    esac
}

## 执行测试
run_tradefed() {
    local mode="${1:-run}"
    cd "$SUITE_PATH" || die "无法进入测试套件目录 $SUITE_PATH"

    local tradefed_bin="./$SUITE_PREFIX-tradefed"
    [[ -x "$tradefed_bin" ]] || die "未找到 tradefed 可执行文件: $tradefed_bin"

    local command="$tradefed_bin run commandAndExit"
    if [[ "$mode" == "retry" ]]; then
        [[ -n "$RESULT_TIMESTAMP" ]] || die "retry 模式缺少 RESULT_TIMESTAMP"
        command="$command retry --retry-result-dir $RESULT_TIMESTAMP"
        log "🔄 Retry 模式, 结果目录: $RESULT_TIMESTAMP"
    else
        command="$command $TEST_COMMAND"
        if [[ -n "$Test_Module" ]]; then
            command="$command -m $Test_Module"
            if [[ -n "$Test_Case" ]]; then
                command="$command -t $Test_Case"
            fi
        fi
    fi
    command="$command $DEVICE_ARGS --disable-reboot"

    log "📋 测试命令: $command"
    log "⏱️ 开始时间: $(date)"
    eval "$command" 2>&1 | tee -a "$LOG_FILE"
    local exit_code=${PIPESTATUS[0]}
    log "⏱️ 结束时间: $(date)"
    log "📊 退出代码: $exit_code"
    return $exit_code
}

## 重新测试
retry_if_needed() {
    (( FAIL_COUNT == 0 )) && return 0
    [[ "$RETRY_FAIL" != "true" ]] && return 0

    if run_tradefed "retry"; then
        log "✅ retry成功"
        return 0
    else
        log "❌ 自动重试失败，回退完整重跑..."
        run_tradefed "run"
    fi
}

## 解析结果
analyze_result() {
    log "🔍 解析结果..."
    cd "$SUITE_PATH" || die "无法进入测试套件目录 $SUITE_PATH"

    local result_dir=$(awk -F': ' '/RESULT DIRECTORY/ {d=$2} END{print d}' "$LOG_FILE" | awk '{print $1}')
    [[ -d "$result_dir" ]] || die "未找到 RESULT DIRECTORY"
    log "📁 结果目录: ${result_dir:-<none>}"
    RESULT_TIMESTAMP=$(basename "$result_dir")

    if [[ -f "$result_dir/test_result.xml" ]]; then
        PASS_COUNT=$(grep -o 'pass="[0-9]*"' "$result_dir/test_result.xml" | head -1 | sed 's/pass="//; s/"//')
        FAIL_COUNT=$(grep -o 'failed="[0-9]*"' "$result_dir/test_result.xml" | head -1 | sed 's/failed="//; s/"//')
    else
        PASS_COUNT=$(awk '/^PASSED[[:space:]]+:/ {print $2}' "$LOG_FILE")
        FAIL_COUNT=$(awk '/^FAILED[[:space:]]+:/ {print $2}' "$LOG_FILE")
    fi
    log "📊 测试结果: PASS: $PASS_COUNT  FAIL: $FAIL_COUNT"
}

## 远程拷贝
copy_to_remote_server() {
    if [[ "$COPY_TO_REMOTE" != "true" ]]; then
        log "📤 远程拷贝已禁用"
        return 0
    fi

    local logs_dir=$(awk -F': ' '/LOG DIRECTORY/ {d=$2} END{print d}' "$LOG_FILE" | awk '{print $1}')
    local result_dir=$(awk -F': ' '/RESULT DIRECTORY/ {d=$2} END{print d}' "$LOG_FILE" | awk '{print $1}')
    [[ -z "$logs_dir" || -z "$result_dir" ]] && die "未找到 RESULT DIRECTORY"
    log "📁 日志目录: ${logs_dir:-<none>}"
    log "📁 结果目录: ${result_dir:-<none>}"

    local timestamp=$(basename "$result_dir")
    [[ -n "$timestamp" ]] || die "无法获取 RESULT_TIMESTAMP"

    local remote_target_dir="/home/$REMOTE_USER/gms_test_results/$timestamp"
    log "🌐 本地主机: ${REMOTE_USER}@${REMOTE_HOST}:${remote_target_dir}"

    # 添加路由
    #######################################
    # Ubuntu主机执行下面命令免密
    # sudo visudo
    # hcq ALL=(root) NOPASSWD: /sbin/ip route add *, /sbin/ip route del *
    #######################################
    if ! ip route show | grep -q "10.10.10.0/24"; then
        log "🛠️ 添加路由: 10.10.10.0/24 via 172.16.14.1"
        sudo -n ip route add 10.10.10.0/24 via 172.16.14.1 || {
            log "❌ 无法添加路由（请配置 sudo NOPASSWD）"
            return 1
        }
    fi

    # 验证 SSH 连接
    if ! ssh -o BatchMode=yes -o ConnectTimeout=5 \
            "${REMOTE_USER}@${REMOTE_HOST}" "echo 'OK' >/dev/null" 2>/dev/null; then
        log "❌ 无法连接远程服务器（检查网络和SSH免密）"
        return 1
    fi

    # 创建远程目录
    ssh "${REMOTE_USER}@${REMOTE_HOST}" "mkdir -p '$remote_target_dir'" 2>&1 | tee -a "$LOG_FILE"

    log "📤 开始拷贝: $remote_target_dir"
    for src in "$logs_dir" "$result_dir"; do
        if [[ -d "$src" ]]; then
            rsync -avz --chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r \
                "$src/" \
                "${REMOTE_USER}@${REMOTE_HOST}:${remote_target_dir}/" \
                2>&1 | tee -a "$LOG_FILE"
        fi
    done
    log "✅ 拷贝完成: ${REMOTE_USER}@${REMOTE_HOST}:${remote_target_dir}"
}

## 主函数
main() {
    parse_args "$@"

    log "🚀 开始测试: $Test_Type"
    log "📦 测试模块: $Test_Module"
    log "🧪 测试用例: $Test_Case"
    log "📱 测试设备: $DEVICE_ARGS"
    log "📁 测试套件: $SUITE_PATH"
    log "🌐 本地主机: ${REMOTE_USER}@${REMOTE_HOST}"
    log "📋 日志文件: $LOG_FILE"
    log "========================================"

    if [[ "$MODE" == "retry" ]]; then
        run_tradefed "retry"
        copy_to_remote_server
        exit $?
    fi
    
    if run_tradefed "run"; then
        analyze_result
        retry_if_needed
        copy_to_remote_server
        log "✅ GMS 测试成功完成"
    else
        log "❌ GMS 测试执行失败"
        copy_to_remote_server
        exit 1
    fi
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi

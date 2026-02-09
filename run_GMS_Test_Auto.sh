#!/bin/bash
set -euo pipefail

export PATH="/home/hcq/Software/sdk_tools_new/sdk_tools:$PATH"

# 可通过环境变量覆盖默认的远程拷贝目标
REMOTE_HOST="${REMOTE_HOST:-10.10.10.206}"
REMOTE_USER="${REMOTE_USER:-hcq}"

## 全局配置
CTS_Suite_PATH="/home/hcq/GMS-Suite/android-cts-16_r3-1/android-cts/tools"
GTS_Suite_PATH="/home/hcq/GMS-Suite/android-gts-13.1-R2/android-gts/tools"
STS_Suite_PATH="/home/hcq/GMS-Suite/android-sts-15_sts-r47/android-sts/tools"
VTS_Suite_PATH="/home/hcq/GMS-Suite/android-vts-16_R3/android-vts/tools"

RETRY_FAIL=true
COPY_TO_REMOTE=true

LOG_FILE="/tmp/gms_test_$(date +%Y%m%d_%H%M%S).log"

## 运行状态
SUITE_PATH=""
SUITE_PATH_USER=""
Suite_PREFIX=""
TEST_COMMAND=""
SHARD_ARGS=""
DEVICE_ARGS=""

MODE="run"
PASS_COUNT=""
FAIL_COUNT=""
RESULT_TIMESTAMP=""

## 工具函数
log() { echo -e "$*" | tee -a "$LOG_FILE"; }
die() { log "❌ $*"; exit 1; }

## 显示帮助
show_help() {
cat <<EOF
用法:
  $0 <cts|gsi|gts|sts|vts|apts> [模块] [用例]
  $0 <cts|gsi|gts|sts|vts|apts> retry <RESULT_TIMESTAMP>

选项:
  --no-retry           禁用失败自动 retry
  --copy-remote        结果拷贝到远端
  --device-args ARGS   后续参数全部透传给 tradefed
  --test-suite PATH    指定自定义测试套件目录（覆盖默认）
  --help               显示帮助

示例:
  $0 cts
  $0 cts CtsSecurityTestCases
  $0 cts retry 2026.01.12_14.36.17.772_8696
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
                RETRY_FAIL=false
                log "✅ 禁用自动重试"
                shift
                ;;

            --copy-remote)
                COPY_TO_REMOTE=true
                log "✅ 启用结果拷贝到远程"
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
                log "📱 设备参数: '$DEVICE_ARGS'"
                ;;
                
            --test-suite)
                shift
                if [[ $# -eq 0 ]]; then
                    die "--test-suite 缺少路径参数"
                fi
                SUITE_PATH_USER="$1"
                log "📁 自定义测试套件路径: $SUITE_PATH_USER"
                shift
                ;;

            --local-server)
                shift
                if [[ $# -eq 0 ]]; then
                    die "--local-server 缺少本地主机配置（格式: user@host）"
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

            -*)
                die "未知参数: $1"
                ;;

            *)
                # 位置参数
                args+=("$1")
                shift
                ;;
        esac
    done

    # 验证必需参数
    if (( ${#args[@]} < 1 )); then
        die "缺少测试类型"
    fi

    Test_Type="${args[0],,}"  # 转换为小写
    Test_Module="${args[1]:-}"
    Test_Case="${args[2]:-}"

    # 处理 retry 模式
    if [[ "${Test_Module,,}" == "retry" ]]; then
        MODE="retry"
        RESULT_TIMESTAMP="$Test_Case"
        if [[ -z "$RESULT_TIMESTAMP" ]]; then
            die "retry 必须指定 RESULT_TIMESTAMP"
        fi
        Test_Module=""
        Test_Case=""
        log "🔄 Retry 模式: $RESULT_TIMESTAMP"
    else
        MODE="run"
        log "🧪 测试配置: 类型=$Test_Type, 模块=$Test_Module, 用例=$Test_Case"
    fi
}

auto_select_suite() {
    # === 第一步：确定最终 SUITE_PATH ===
    if [[ -n "$SUITE_PATH_USER" ]]; then
        SUITE_PATH="$SUITE_PATH_USER"
        log "📁 使用自定义测试套件路径: $SUITE_PATH"
    else
        # 使用默认映射
        case "$Test_Type" in
            cts|gsi)  SUITE_PATH="$CTS_Suite_PATH" ;;
            gts|apts) SUITE_PATH="$GTS_Suite_PATH" ;;
            sts)      SUITE_PATH="$STS_Suite_PATH" ;;
            vts)      SUITE_PATH="$VTS_Suite_PATH" ;;
            *)        die "不支持的测试类型: $Test_Type" ;;
        esac
        log "📁 使用默认测试套件路径: $SUITE_PATH"
    fi

    # 校验路径是否存在
    [[ -d "$SUITE_PATH" ]] || die "测试套件目录不存在: $SUITE_PATH"

    # === 第二步：自动检测 Suite_PREFIX ===
    case "$Test_Type" in
        cts|gsi)  Suite_PREFIX="cts" ;;
        gts|apts) Suite_PREFIX="gts" ;;
        sts)      Suite_PREFIX="sts" ;;
        vts)      Suite_PREFIX="vts" ;;
        *)        Suite_PREFIX="cts" ;;
    esac

    # 验证 tradefed 可执行文件
    local tradefed_path="$SUITE_PATH/${Suite_PREFIX}-tradefed"
    if [[ ! -x "$tradefed_path" ]]; then
        die "未找到 tradefed 可执行文件: $tradefed_path"
    fi
    log "✅ 找到 tradefed: $tradefed_path"

    # === 第三步：设置 TEST_COMMAND ===
    case "$Test_Type" in
        cts)       TEST_COMMAND="cts" ;;
        gsi)       TEST_COMMAND="cts-on-gsi" ;;
        gts)       TEST_COMMAND="gts" ;;
        sts)       TEST_COMMAND="sts-dynamic-full" ;;
        vts)       TEST_COMMAND="vts" ;;
        apts)      TEST_COMMAND="apts" ;;
        *)         die "不支持的测试类型: $Test_Type" ;;
    esac
}

## 检查设备
detect_devices() {
    log "🔍 检查设备..."
    adb wait-for-device

    mapfile -t DEVICES < <(adb devices | awk '$2=="device"{print $1}')
    
    if (( ${#DEVICES[@]} == 0 )); then
        die "未检测到任何在线设备"
    fi

    # 构建设备参数
    if (( ${#DEVICES[@]} == 1 )); then
        SHARD_ARGS="-s ${DEVICES[0]}"
    else
        SHARD_ARGS="--shard-count ${#DEVICES[@]}"
        for d in "${DEVICES[@]}"; do
            SHARD_ARGS+=" -s $d"
        done
    fi
    log "📱 连接设备: (${#DEVICES[@]}) ${DEVICES[*]}"
}

## 执行测试
run_test() {
    cd "$SUITE_PATH" || die "无法进入 $SUITE_PATH"

    local command="./$Suite_PREFIX-tradefed run commandAndExit $TEST_COMMAND $SHARD_ARGS"
    if [[ -n "$Test_Module" ]]; then
        command="$command -m $Test_Module"
    fi
    if [[ -n "$Test_Case" ]]; then
        command="$command -t $Test_Case"
    fi
    command="$command --disable-reboot"

    log "📋 测试命令: $command"
    log "⏱️ 开始时间: $(date)"
    eval "$command" 2>&1 | tee -a "$LOG_FILE"
    local exit_code=${PIPESTATUS[0]}
    log "⏹️ 结束时间: $(date)"
    log "📊 退出代码: $exit_code"
    log "========================================"
    
    return $exit_code
}

## 直接 Retry
run_retry_with_result_dir() {
    cd "$SUITE_PATH" || die "无法进入 $SUITE_PATH"

    local tf_bin="./$Suite_PREFIX-tradefed"
    [[ -x "$tf_bin" ]] || die "未找到 tradefed: $tf_bin"

    log "🔄 Retry by result dir: $RESULT_TIMESTAMP"
    log "📋 测试命令: $tf_bin run commandAndExit retry --retry-result-dir $RESULT_TIMESTAMP $SHARD_ARGS"
    log "⏱️ 开始时间: $(date)"

    $tf_bin run commandAndExit retry --retry-result-dir $RESULT_TIMESTAMP $SHARD_ARGS 2>&1 | tee -a "$LOG_FILE"
    local exit_code=${PIPESTATUS[0]}

    log "⏹️ 结束时间: $(date)"
    log "📊 Retry 退出码: $exit_code"
    return $exit_code
}

## 解析结果
analyze_result() {
    log "🔍 解析结果..."
    cd "$SUITE_PATH" || die "无法进入 $SUITE_PATH"

    local logs_dir=$(awk -F': ' '/LOG DIRECTORY/ {d=$2} END{print d}' "$LOG_FILE" | awk '{print $1}')
    local result_dir=$(awk -F': ' '/RESULT DIRECTORY/ {d=$2} END{print d}' "$LOG_FILE" | awk '{print $1}')

    log "📁 日志目录: ${logs_dir:-<none>}"
    log "📁 结果目录: ${result_dir:-<none>}"

    [[ -d "$result_dir" ]] || die "未找到 RESULT DIRECTORY"

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

## 重新测试
retry_if_needed() {
    (( FAIL_COUNT == 0 )) && return 0
    [[ "$RETRY_FAIL" != true ]] && return 0

    if run_retry_with_result_dir; then
        log "✅ retry成功"
        return 0
    else
        log "❌ 自动重试失败，回退完整重跑..."
        run_test
    fi
}

## 远程拷贝
copy_to_remote_server() {
    if [[ "$COPY_TO_REMOTE" != true ]]; then
        log "📤 远程拷贝已禁用"
        return 0
    fi

    local logs_dir=$(awk -F': ' '/LOG DIRECTORY/ {d=$2} END{print d}' "$LOG_FILE" | awk '{print $1}')
    local result_dir=$(awk -F': ' '/RESULT DIRECTORY/ {d=$2} END{print d}' "$LOG_FILE" | awk '{print $1}')
    log "📁 日志目录: ${logs_dir:-<none>}"
    log "📁 结果目录: ${result_dir:-<none>}"

    [[ -z "$logs_dir" || -z "$result_dir" ]] && die "未找到 RESULT DIRECTORY"

    # ✅ 从 result_dir 提取时间戳（可靠！）
    local RESULT_TIMESTAMP=$(basename "$result_dir")
    [[ -n "$RESULT_TIMESTAMP" ]] || die "无法获取 RESULT_TIMESTAMP"

    local remote_host="$REMOTE_HOST"
    local remote_user="$REMOTE_USER"
    local remote_target_dir="/home/$remote_user/gms_test_results/$RESULT_TIMESTAMP"

    log "🌐 本地主机: ${remote_user}@${remote_host}:${remote_target_dir}"

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
            "${remote_user}@${remote_host}" "echo 'OK' >/dev/null" 2>/dev/null; then
        log "❌ 无法连接远程服务器（检查网络和SSH免密）"
        return 1
    fi

    # 创建远程目录
    ssh "${remote_user}@${remote_host}" "mkdir -p '$remote_target_dir'" 2>&1 | tee -a "$LOG_FILE"

    log "📤 开始拷贝: $remote_target_dir"

    # 同步目录
    for src in "$logs_dir" "$result_dir"; do
        if [[ -d "$src" ]]; then
            rsync -avz --chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r \
                "$src/" \
                "${remote_user}@${remote_host}:${remote_target_dir}/" \
                2>&1 | tee -a "$LOG_FILE"
        fi
    done

    log "✅ 拷贝完成: ${remote_user}@${remote_host}:${remote_target_dir}"
}

## 主函数
main() {
    parse_args "$@"
    auto_select_suite

    if [[ -n "$DEVICE_ARGS" ]]; then
        SHARD_ARGS="$DEVICE_ARGS"
        log "📱 使用外部设备参数: $SHARD_ARGS"
    else
        detect_devices
    fi

    log "🚀 开始测试: $Test_Type"
    log "📦 测试模块: $Test_Module"
    log "🧪 测试用例: $Test_Case"
    log "📱 测试设备: $SHARD_ARGS"
    log "📁 测试套件: $SUITE_PATH"
    log "📋 日志文件: $LOG_FILE"
    log "========================================"

    if [[ "$MODE" == "retry" ]]; then
        run_retry_with_result_dir
        copy_to_remote_server
        exit $?
    fi

    # 执行主测试
    if run_test; then
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

# 确保脚本被正确调用
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi

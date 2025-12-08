#!/bin/bash
set -e
# 解决中文乱码（可选，适配企业微信/WorkTool 中文显示）
export LANG=C.UTF-8
export LC_ALL=C.UTF-8

echo "=== Waiting for Android boot... ==="
ADB_WAIT_TIMEOUT=600  # 超时时间（秒）
ADB_WAIT_INTERVAL=5   # 检查间隔（秒）
ELAPSED_TIME=0

# 循环等待 ADB 就绪（容器内默认端口 5555）
while [ $ELAPSED_TIME -lt $ADB_WAIT_TIMEOUT ]; do
    # 先确保 adb server 启动
    adb start-server > /dev/null 2>&1
    # 检查容器内模拟器是否可连接（关键：用 127.0.0.1 而非 localhost，避免解析问题）
    if adb connect 127.0.0.1:5555 2>&1 | grep -q "connected"; then
        echo "=== ADB connected successfully ==="
        break
    fi
    echo "Waiting for ADB... (elapsed: $ELAPSED_TIME/$ADB_WAIT_TIMEOUT sec)"
    sleep $ADB_WAIT_INTERVAL
    ELAPSED_TIME=$((ELAPSED_TIME + ADB_WAIT_INTERVAL))
done

# 超时判断：若未连接成功，终止脚本
if [ $ELAPSED_TIME -ge $ADB_WAIT_TIMEOUT ]; then
    echo "ERROR: ADB connection timed out after $ADB_WAIT_TIMEOUT seconds"
    # 终止原入口脚本，容器退出
    kill $ENTRYPOINT_PID
    exit 1
fi

echo "=== Waiting for Android system ready... ==="
sleep 10

# 安装 APK 函数（优化：支持覆盖安装、处理安装失败）
install_if_needed() {
    local pkg_name=$1
    local apk_path=$2
    # 检查 APK 文件是否存在
    if [ ! -f "$apk_path" ]; then
        echo "ERROR: APK file $apk_path not found!"
        return 1
    fi
    # 检查是否已安装
    if adb -s 127.0.0.1:5555 shell pm list packages | grep -q "$pkg_name"; then
        echo "✅ $pkg_name already installed"
        return 0
    fi
    # 安装 APK（-r 允许覆盖安装，-d 允许降级安装）
    echo "📦 Installing $apk_path ..."
    if adb -s 127.0.0.1:5555 install -r -d "$apk_path"; then
        echo "✅ $pkg_name installed successfully"
    else
        echo "❌ Failed to install $pkg_name"
        # 尝试安装 ARM 兼容库（解决 x86 模拟器运行 ARM APK 问题）
        echo "Trying to install ARM compatibility library..."
        apt update && apt install -y libhoudini86
        # 重新安装
        adb install -r -d "$apk_path"
    fi
}

# 安装企业微信和 WorkTool
install_if_needed "com.tencent.wework" "/apks/wework.apk"
install_if_needed "com.worktool.app" "/apks/worktool-2.8.0.apk"

# 启动 WorkTool（优化：用 am start 而非 monkey，更稳定）
echo "🚀 Launching WorkTool..."
adb -s 127.0.0.1:5555 shell am start -n "com.worktool.app/.MainActivity" -a android.intent.action.MAIN -c android.intent.category.LAUNCHER || {
    echo "WARNING: Failed to launch WorkTool (may not affect usage)"
}

# 启动企业微信（可选，自动登录前先启动）
echo -s 127.0.0.1:5555 "🚀 Launching WeWork..."
adb shell am start -n "com.tencent.wework/.launch.LaunchSplashActivity" || {
    echo "WARNING: Failed to launch WeWork (may not affect usage)"
}

echo "=== All services ready! ==="
# 如果需要在安卓容器内直接运行监控脚本，尝试确保 python3 可用并以后台循环方式启动
if [ "${ENABLE_MONITOR,,}" = "true" ]; then
    echo "=== ENABLE_MONITOR is true: preparing python environment ==="
    if ! command -v python3 >/dev/null 2>&1; then
        echo "python3 not found, installing..."
        apt-get update && apt-get install -y python3 python3-pip || true
    fi
    # 安装脚本运行需要的 Python 包（非重复安装）
    if python3 -c "import selenium" >/dev/null 2>&1; then
        echo "selenium already installed"
    else
        pip3 install --no-cache-dir selenium requests pyyaml python-dotenv pytz || true
    fi

    INTERVAL=${MONITOR_INTERVAL:-300}
    echo "=== Starting monitor loop in background (interval=${INTERVAL}s) ==="
    (
        while true; do
            echo "[monitor] $(date +'%Y-%m-%d %H:%M:%S') Running /scripts/monitor_appointment.py"
            python3 /scripts/monitor_appointment.py || echo "[monitor] script exited with $?"
            sleep ${INTERVAL}
        done
    ) &
fi
# 保持容器运行（等待原入口脚本结束）
wait $ENTRYPOINT_PID
# 羽毛球场监测程序

这是一个用于监测羽毛球场预约页面的自动化程序。程序会定时打开目标预约页面，检查是否存在可预约场地，并在满足条件时通过配置的通知渠道发送提醒。

## 项目结构

```text
.
├── app/                    # Python 监测程序
│   └── monitor_appointment.py
├── scripts/                # 启动脚本
│   └── monitor_loop.sh
├── docker/                 # Docker 构建文件
│   └── Dockerfile.monitor
├── data/                   # 运行数据，例如 cookies、提醒记录
├── .example.env            # 配置示例
├── .env                    # 实际运行配置，本地自行创建
└── docker-compose.yml
```

## 配置

具体参数都在 `.env` 文件中设置。首次使用时，将 `.example.env` 复制或重命名为 `.env`，然后在 `.env` 中填写实际参数。

Windows PowerShell：

```powershell
Copy-Item .example.env .env
```

Linux/macOS：

```bash
cp .example.env .env
```

常用配置包括：

- `TARGET_URL`：需要监测的预约页面地址
- `NKU_USERNAME` / `NKU_PASSWORD`：登录账号和密码
- `FEISHU_WEBHOOK_URL_PERSON` / `FEISHU_WEBHOOK_URL_GROUP`：飞书通知 Webhook
- `BEGIN_HOUR` / `END_HOUR`：允许监测的时间范围
- `INCLUDE_MORNING`：是否包含上午时段
- `LEAST_TIME_LENGTH`：最短可用连续时长,单位为小时
- `MEMORY_THRESHOLD`：同一时段当天重复提醒阈值
- `MONITOR_INTERVAL`：监测间隔，单位为秒

## 运行方式一：Docker(推荐)

推荐使用 Docker 运行。请在项目根目录执行：

```powershell
docker compose up -d
```

停止服务：

```powershell
docker compose down
```

查看日志：

```powershell
docker compose logs -f monitor
```

## 运行方式二：直接运行 Python

也可以不使用 Docker，直接循环运行 `app/` 下的 Python 文件。

先安装依赖：

```powershell
pip install selenium requests pyyaml python-dotenv pytz
```

单次运行：

```powershell
python app/monitor_appointment.py
```

循环运行示例。

Windows PowerShell：

```powershell
while ($true) {
    python app/monitor_appointment.py
    Start-Sleep -Seconds 300
}
```

Linux/macOS：

```bash
while true; do
    python app/monitor_appointment.py
    sleep 300
done
```

如果需要修改循环间隔，可以将 `300` 改成你需要的秒数，或者参考 `.env` 中的 `MONITOR_INTERVAL`。

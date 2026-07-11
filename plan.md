请帮我实现一个轻量级的 Linux 多用户服务器监控项目，项目名为：

```text
simple-node-sentinel
```

目标是做一个简单、稳定、容易部署的监控服务。不要过度设计，不使用 Docker、Prometheus、Grafana、Redis、PostgreSQL 或复杂前端框架，也不需要控制风扇、杀进程、重启 GPU 或执行任何系统命令。

## 一、主要功能

服务运行在 Ubuntu 多用户 NVIDIA GPU 服务器上，需要完成：

1. 每隔约 2 秒采集一次服务器状态；
2. 监控所有 NVIDIA GPU；
3. 显示每张 GPU 上的进程 PID、Linux 用户、命令和显存占用；
4. 监控 CPU 使用率和 CPU 温度；
5. 监控系统内存和 Swap；
6. 监控各个实际挂载磁盘的容量和使用率；
7. 按用户汇总 CPU、内存、GPU 进程数和 GPU 显存占用；
8. 提供一个简单的只读网页；
9. 当某张 GPU 连续 5 分钟超过 85°C 时，向相关用户和管理员发送邮件；
10. 使用 systemd 以 root 身份开机自启动；
11. 网站只监听 `127.0.0.1:8080`，用户通过 SSH 隧道访问。

## 二、技术栈

使用：

* Python 3.10+
* FastAPI
* Uvicorn
* psutil
* nvidia-ml-py
* PyYAML
* Python 标准库 `sqlite3`
* 原生 HTML、CSS 和 JavaScript
* systemd

不要在 `requirements.txt` 中加入 `sqlite3`，因为它属于 Python 标准库。

## 三、建议项目结构

```text
simple-node-sentinel/
├── README.md
├── requirements.txt
├── config.example.yaml
├── simple_node_sentinel/
│   ├── __init__.py
│   ├── main.py
│   ├── config.py
│   ├── gpu_monitor.py
│   ├── system_monitor.py
│   ├── process_monitor.py
│   ├── database.py
│   ├── alert_manager.py
│   ├── email_sender.py
│   └── web/
│       ├── index.html
│       ├── app.js
│       └── style.css
├── scripts/
│   └── install.sh
└── systemd/
    └── simple-node-sentinel.service
```

保持代码结构清楚即可，不需要创建过多抽象层。

## 四、GPU 监控

使用 NVML 获取每张 GPU 的：

* GPU index
* UUID
* GPU 名称
* GPU 使用率
* 显存总量、已使用量和空闲量
* GPU 温度
* 风扇速度
* 当前功耗
* 功耗上限
* 当前 compute process
* 每个 GPU PID 使用的显存

风扇只读取，不做任何控制。

如果风扇、功耗或其他字段不受支持，返回 `null` 或 `"N/A"`，程序不能崩溃。

## 五、GPU 进程信息

对于每个 GPU PID，读取：

* PID
* Linux 用户名和 UID
* 完整命令行
* 可执行文件名
* 进程启动时间
* 已运行时间
* CPU 使用率
* 系统内存 RSS
* GPU index
* GPU UUID
* GPU 显存占用

需要正确处理：

* 进程在采集过程中退出；
* `psutil.NoSuchProcess`；
* `psutil.AccessDenied`；
* `/proc/<PID>` 已不存在。

单个进程读取失败时直接跳过，不能让整个服务停止。

不要读取 `/proc/<PID>/environ`，不要读取或显示用户环境变量。

命令行中以下参数需要脱敏：

```text
--password
--passwd
--token
--api-key
--apikey
--secret
--access-key
--wandb-api-key
--hf-token
```

例如：

```text
python train.py --token abcdef
```

网页中显示为：

```text
python train.py --token ********
```

## 六、CPU、温度、内存和用户统计

使用 `psutil` 监控：

### CPU

* 总 CPU 使用率
* 每个逻辑 CPU 核心的使用率
* 逻辑核心数量
* Load Average：1、5、15 分钟
* 系统启动时间

### CPU 温度

优先使用：

```python
psutil.sensors_temperatures(fahrenheit=False)
```

显示 Linux 能够读取到的 CPU 温度，例如：

* `Tctl`
* `Tdie`
* `Tccd`
* `Package id`
* `Core`

同时计算并显示最高 CPU 温度。

不同硬件的传感器名称不同，不要只支持某一种 CPU。

如果系统无法读取 CPU 温度，返回：

```json
{
  "available": false,
  "max_celsius": null,
  "sensors": []
}
```

不要因此报错退出。

### 内存

监控：

* 总内存
* 已使用内存
* 可用内存
* 内存使用率
* Swap 总量
* Swap 已使用量
* Swap 使用率

### 用户资源汇总

按 Linux 用户统计：

* 总进程数
* 总 CPU 使用率
* 总内存 RSS
* GPU 进程数
* GPU 显存总占用

第一版不需要显示所有普通进程的完整列表，只显示：

1. GPU 进程详情；
2. 每个用户的资源汇总。

## 七、磁盘监控

监控所有实际挂载磁盘，例如：

```text
/
/home
/data/ssd1
/data/ssd2
/data/ssd3
```

显示：

* 设备名称
* 挂载路径
* 文件系统类型
* 总容量
* 已使用容量
* 可用容量
* 使用率

忽略虚拟文件系统，例如：

```text
proc
sysfs
tmpfs
devtmpfs
devpts
cgroup
cgroup2
overlay
squashfs
```

磁盘信息每 10 秒更新一次即可。

如果某个挂载路径暂时不可访问或磁盘掉挂载，记录错误并继续运行，不要让服务退出。

## 八、网页和 API

使用 FastAPI 提供简单只读网页。

服务必须只监听：

```text
127.0.0.1:8080
```

不要监听：

```text
0.0.0.0
```

提供以下接口：

```text
GET /api/summary
GET /api/gpus
GET /api/gpu-processes
GET /api/users
GET /api/disks
GET /api/alerts
GET /api/history?range_seconds=3600&max_points=720
GET /health
```

网页每 2 秒刷新一次实时状态，历史曲线每 30 秒刷新一次。历史接口必须在服务端
按时间桶聚合，单个序列最多返回 1000 个点，避免将数十万个原始点发送到浏览器。

首页包含：

1. 系统总览
   CPU 使用率、CPU 温度、Load Average、内存、Swap、系统运行时间。

2. GPU 卡片
   GPU 编号、名称、温度、风扇、使用率、显存、功耗、用户和进程数。

3. GPU 进程表
   GPU、用户、PID、GPU 显存、CPU、系统内存、运行时间和脱敏后的命令。

4. 用户资源表
   用户名、进程数、CPU、内存、GPU 进程数和 GPU 显存。

5. 磁盘卡片
   设备、挂载路径、文件系统、总容量、已使用、可用、使用率和历史曲线。

6. 告警区域
   GPU、告警时间、当前温度、最高温度、相关用户和邮件状态。

系统、GPU 和磁盘指标在对应卡片内显示最近 15 分钟、1 小时、6 小时、24 小时
或 3 天的曲线。图表资源随项目本地提供，不依赖公网 CDN。页面保持简单、清楚，
不需要登录系统、动画或复杂前端框架。

网页必须完全只读，不提供任何 `POST` 控制接口。

## 九、SQLite

使用 Python 标准库中的 `sqlite3`。

数据库路径从 YAML 配置中读取，可以由用户自定义，但必须是绝对路径。

默认配置：

```yaml
database:
  path: /var/lib/simple-node-sentinel/simple-node-sentinel.db
  retention_days: 3
  cleanup_interval_seconds: 3600
```

服务启动时自动创建数据库父目录。

SQLite 使用：

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
```

数据库保存：

* 进程首次发现和最后发现记录；
* GPU 高温告警事件；
* 邮件发送记录；
* CPU、内存和 Swap 指标（默认每 2 秒）；
* 每张 GPU 的利用率、显存、温度、风扇和功耗（默认每 2 秒）；
* 每个物理挂载点的容量和使用率（默认每 10 秒）。

指标使用按类型拆分的宽表，不使用 EAV 或逐指标 JSON 行。每轮系统和 GPU 指标在
同一个事务中写入；磁盘只在实际采样时写入，不重复保存旧值。实时状态仍保存在
内存中，以便网页低延迟刷新。

每小时清理一次历史记录，只保留最近 3 天的数据。

清理规则：

* 删除结束时间超过 3 天的进程记录；
* 删除恢复时间超过 3 天的告警记录；
* 删除创建时间超过 3 天的邮件记录；
* 删除采样时间超过 3 天的系统、GPU 和磁盘指标；
* 不删除仍在运行的进程；
* 不删除尚未恢复的告警。

数据库路径可以改成其他位置，例如：

```yaml
database:
  path: /data/ssd3/simple-node-sentinel/simple-node-sentinel.db
```

## 十、GPU 高温邮件告警

每张 GPU 独立维护告警状态。

规则：

```text
GPU 温度 > 85°C 持续 5 分钟：
    发送一次告警邮件

温度持续高于阈值：
    每 60 分钟最多提醒一次

温度 < 80°C 持续 5 分钟：
    标记恢复
    可以发送一次恢复邮件
```

使用 `time.monotonic()` 判断持续时间。

在 GPU 超温的 5 分钟内，记录所有使用过这张 GPU 的用户，而不是只记录告警触发瞬间的用户。

邮件发送给：

* 相关用户；
* 管理员。

用户邮箱通过 YAML 显式配置，不要自动猜测邮箱地址。

如果某个用户没有邮箱映射，只通知管理员，并在邮件中注明。

示例配置：

```yaml
alerts:
  high_temperature_celsius: 85
  high_duration_seconds: 300
  recovery_temperature_celsius: 80
  recovery_duration_seconds: 300
  reminder_interval_seconds: 3600

email:
  enabled: false
  smtp_host: smtp.example.com
  smtp_port: 587
  use_starttls: true
  username: monitor@example.com
  password_file: /etc/simple-node-sentinel/smtp-password
  from_address: monitor@example.com
  admin_emails:
    - admin@example.com

users:
  dingming:
    email: dingming@example.com
  rihan:
    email: rihan@example.com
```

SMTP 密码必须从独立文件读取，不要直接写入 YAML。

当 `email.enabled` 为 `false` 时，只记录告警，不发送邮件。

## 十一、systemd 和 root 运行

服务需要以 root 身份运行，因为要读取其他用户的进程信息。

systemd 服务名：

```text
simple-node-sentinel.service
```

默认目录：

```text
项目目录：/opt/simple-node-sentinel
配置目录：/etc/simple-node-sentinel
数据目录：/var/lib/simple-node-sentinel
```

systemd 服务至少包含：

```ini
[Unit]
Description=Simple Node Sentinel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=/opt/simple-node-sentinel

ExecStart=/opt/simple-node-sentinel/venv/bin/python \
    -m simple_node_sentinel.main \
    --config /etc/simple-node-sentinel/config.yaml

Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes

ReadWritePaths=/var/lib/simple-node-sentinel
ReadOnlyPaths=/etc/simple-node-sentinel

[Install]
WantedBy=multi-user.target
```

不要使用可能阻止访问 `/dev/nvidia*` 的 `PrivateDevices=yes`。

程序需要正确处理 `SIGTERM` 和 Ctrl+C，并在退出时关闭 NVML 和 SQLite。

## 十二、SSH 浏览器访问

README 中说明：浏览器本身不直接使用 SSH，需要先建立 SSH 本地端口转发。

普通 SSH 端口：

```bash
ssh -N -L 127.0.0.1:8080:127.0.0.1:8080 username@server
```

SSH 端口为 2255：

```bash
ssh -p 2255 -N \
  -L 127.0.0.1:8080:127.0.0.1:8080 \
  username@server
```

保持 SSH 连接打开，然后在本地浏览器访问：

```text
http://127.0.0.1:8080
```

如果本地 8080 已被占用，可以使用：

```bash
ssh -p 2255 -N \
  -L 127.0.0.1:18080:127.0.0.1:8080 \
  username@server
```

然后访问：

```text
http://127.0.0.1:18080
```

## 十三、安装和 README

提供一个简单的 `scripts/install.sh`，完成：

1. 创建 `/opt/simple-node-sentinel`；
2. 创建 Python venv；
3. 安装 requirements；
4. 创建 `/etc/simple-node-sentinel`；
5. 创建 `/var/lib/simple-node-sentinel`；
6. 安装 systemd service；
7. 执行 `systemctl daemon-reload`；
8. 提示用户修改配置文件；
9. 不要默认启用邮件发送。

README 中写明：

```bash
sudo apt install lm-sensors sqlite3
sudo sensors-detect --auto
sensors
```

其中 `sqlite3` 命令行工具是可选的，Python 程序本身不依赖该命令行工具。

同时提供：

```bash
python3 -c "import sqlite3; print(sqlite3.sqlite_version)"
```

以及启动和查看日志的命令：

```bash
sudo systemctl enable --now simple-node-sentinel.service
sudo systemctl status simple-node-sentinel.service
sudo journalctl -u simple-node-sentinel.service -f
```

## 十四、代码要求

* 使用类型标注；
* 保持代码简单；
* 单个采集错误不能导致服务退出；
* 不执行 shell 命令处理用户输入；
* 不提供任意文件读取接口；
* 不提供杀进程接口；
* 不控制风扇；
* 不修改 GPU；
* 不自动关机；
* 不读取环境变量；
* 日志中不输出密码、Token 或未脱敏命令；
* 所有 API 都是只读的；
* 不加入暂时没有要求的复杂功能。

请先检查当前目录是否已经有项目代码。如果已有代码，在现有结构上修改；如果没有，再创建完整项目。

完成后请：

1. 生成所有必要代码；
2. 生成 `requirements.txt`；
3. 生成 `config.example.yaml`；
4. 生成 systemd 服务文件；
5. 生成安装脚本；
6. 生成 README；
7. 检查项目能否启动；
8. 说明启动命令、配置方式、SSH 访问方式和测试方法。

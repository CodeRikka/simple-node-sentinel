# Simple Node Sentinel

A small monitor and GPU fan controller for multi-user Ubuntu NVIDIA GPU
servers. It uses FastAPI, psutil, NVML and SQLite, with a dependency-free
browser interface. It never kills processes or executes commands received
from users. Fan changes use a fixed NVML API with validated values.

## What it monitors

- CPU usage, per-core usage, load average, uptime and available temperatures
- Memory and swap
- Physical disk mounts (virtual filesystems are ignored)
- NVIDIA GPU utilization, memory, temperature, fan, power and compute processes
- GPU process owner, sanitized command, runtime, CPU, RAM and GPU memory
- Per-user process, CPU, RAM and GPU totals
- Sustained GPU temperature alerts and optional SMTP delivery
- Per-GPU NVIDIA automatic or 60–90% manual fan control

The latest snapshot stays in memory for fast live updates. SQLite also stores
system, GPU and disk metric history, GPU process lifetimes, temperature alert
events and email delivery records. Metric history is retained for three days by
default and is downsampled by the API before it reaches the browser.

## Requirements

- Ubuntu with Python 3.10 or newer
- NVIDIA driver providing NVML
- Root for production, so processes belonging to other users can be inspected
- A GPU/driver combination exposing `nvmlDeviceGetNumFans`,
  `nvmlDeviceSetFanSpeed_v2` and `nvmlDeviceSetDefaultFanSpeed_v2` for fan
  control. Monitoring continues normally when these calls are unavailable.
- Optional CPU sensor setup:

```bash
sudo apt install lm-sensors sqlite3
sudo sensors-detect --auto
sensors
```

The `sqlite3` command is optional. Python uses its standard-library module:

```bash
python3 -c "import sqlite3; print(sqlite3.sqlite_version)"
```

## Configuration

`config.example.yaml` keeps site-specific fields empty and documents how to fill
them. The database path must be absolute. Email is disabled by default.

Production config lives at `/etc/simple-node-sentinel/config.yaml`. The
installer copies the example only when that file does not already exist, then
interactively offers to fill empty fields. Press Enter to skip any prompt.

If `/etc/simple-node-sentinel/smtp-password` is missing, the installer creates
an empty `0600` file and asks whether to fill the SMTP password. You can also
edit either file later:

```bash
sudoedit /etc/simple-node-sentinel/config.yaml
sudoedit /etc/simple-node-sentinel/smtp-password
```

The password file must contain only the SMTP password or app password, with no
quotes. User email addresses are never guessed; add explicit entries under
`users`. Users without mappings are named in the administrator notification.

Configuration is read at process start. After editing `config.yaml` or the
SMTP password file, restart the service (no need to rerun `install.sh`):

```bash
sudoedit /etc/simple-node-sentinel/config.yaml
sudo systemctl restart simple-node-sentinel.service
sudo systemctl status simple-node-sentinel.service
sudo journalctl -u simple-node-sentinel.service -f
```

`database.retention_days` applies to metric history as well as completed event
records. At the default two-second collection interval, multi-GPU nodes can use
several hundred MiB for three days of history; monitor the database file when
choosing a longer retention period.

`fan_control` enables the dashboard controls and defines the allowed manual
range. The supplied configuration allows 60–90% in 5% steps. When a GPU has no
NVML compute processes and its temperature is below 60°C, the service restores
NVIDIA automatic fan control and temporarily disables manual mode. Manual mode
becomes available again when a process appears or the temperature rises above
60°C; it is not automatically re-enabled.

```yaml
fan_control:
  enabled: true
  minimum_percent: 60
  maximum_percent: 90
  step_percent: 5
  idle_temperature_celsius: 60
```

Fan control has no separate login or administrator role. Every user who can
reach the dashboard can change it. Keep the service bound to localhost and
grant SSH access only to trusted users. Set `fan_control.enabled: false` to
retain a read-only deployment.

Temperature alerts are sent after five continuous minutes above the configured
threshold, with at most one alert per GPU every two hours. Recovery is recorded
after five continuous minutes below the recovery threshold, but no recovery
email is sent and the two-hour cooldown is preserved. On service startup,
persisted active alerts are compared with the first available GPU temperature
sample; alerts already below the recovery threshold are immediately marked
recovered. Recovered records are immutable and are never changed back to
active—a later high-temperature event creates a new alert.

To notify selected users when one of their GPU processes ends, list their Linux
usernames under `process_end_notifications.users`. The process must have run
for at least `min_runtime_seconds` (default five minutes), then be absent from
NVML for `missing_duration_seconds` (default 20 seconds) before the user is
emailed. Short-lived processes are ignored. These notifications go only to the
affected user, not to administrators.

## Production installation

Review the installer and configuration first. The installer must be run by an
administrator and does not start the service automatically:

```bash
sudo ./scripts/install.sh
# answer prompts for empty fields, or press Enter to skip
sudoedit /etc/simple-node-sentinel/config.yaml   # optional manual review
sudo systemctl enable --now simple-node-sentinel.service
sudo systemctl status simple-node-sentinel.service
sudo journalctl -u simple-node-sentinel.service -f
```

Re-running the installer updates `/opt` and the systemd unit, but does not
overwrite an existing `config.yaml`. Empty fields can still be filled again
through the interactive prompts.

## Uninstall

```bash
sudo ./scripts/uninstall.sh
```

By default this stops/disables the service and removes `/opt/simple-node-sentinel`.
It asks before deleting config and database directories. To remove everything:

```bash
sudo ./scripts/uninstall.sh --purge -y
```

Installed paths:

- Application: `/opt/simple-node-sentinel`
- Configuration: `/etc/simple-node-sentinel`
- Data: `/var/lib/simple-node-sentinel`

The service runs as root but is constrained by systemd hardening. It does not
use `PrivateDevices`, because NVML monitoring and fan control need
`/dev/nvidia*`.

## Unprivileged development

No installation or database is created merely by importing the package. To run
locally, make a temporary config whose database path is `:memory:` or a path in
a disposable temporary directory:

```bash
python -m simple_node_sentinel.main --config /path/to/temporary-config.yaml
```

The server always listens on `127.0.0.1:8080`; binding to `0.0.0.0` is not
supported. Without root, information about other users' processes may be
unavailable. Missing NVML, sensors, fields, processes or mounts are reported as
unavailable and do not stop the service.

## Browser access over SSH

Keep one of these commands running on your local computer:

```bash
ssh -N -L 127.0.0.1:8080:127.0.0.1:8080 username@server
ssh -p 2255 -N -L 127.0.0.1:8080:127.0.0.1:8080 username@server
```

Then open `http://127.0.0.1:8080`. If local port 8080 is busy:

```bash
ssh -p 2255 -N -L 127.0.0.1:18080:127.0.0.1:8080 username@server
```

Open `http://127.0.0.1:18080`.

The dashboard refreshes current values every two seconds. CPU, memory, swap,
GPU and disk cards include historical curves with 15-minute, 1-hour, 6-hour,
24-hour and 3-day ranges. Each GPU card also has a colored fan whose rotation
tracks the reported fan percentage. It is green at 60°C and below, transitions
toward red between 60°C and 90°C, and stays red at 90°C and above. Charts and
icons are served locally and do not require internet access.

## API

- `GET /api/summary`
- `GET /api/gpus`
- `GET /api/gpu-processes`
- `GET /api/users`
- `GET /api/disks`
- `GET /api/alerts`
- `GET /api/history?range_seconds=3600&max_points=720`
- `GET /health`
- `PUT /api/gpus/{gpu_uuid}/fan-control`

The history endpoint accepts 60–259200 seconds and returns at most 1000
downsampled points per series. `GET /api/gpus` includes a `fan_control` object
with `mode`, `target_percent`, `revision`, `manual_allowed`, capability and
error fields.

Use the latest `revision` when changing a fan. For example:

```bash
curl -X PUT http://127.0.0.1:8080/api/gpus/GPU-UUID/fan-control \
  -H 'Content-Type: application/json' \
  -d '{"mode":"manual","target_percent":75,"expected_revision":0}'

curl -X PUT http://127.0.0.1:8080/api/gpus/GPU-UUID/fan-control \
  -H 'Content-Type: application/json' \
  -d '{"mode":"auto","target_percent":null,"expected_revision":1}'
```

Fan writes are serialized. If two users submit from the same revision, the
first successful request wins and the other receives HTTP 409 with the newest
state. A failed NVML call does not update the stored mode or revision.

The last successful mode and target are stored by GPU UUID in SQLite. On
service or machine restart, the service reads that state and reapplies it after
the first GPU sample. The idle/low-temperature rule takes priority: a persisted
manual mode is changed to automatic instead of being restored when the GPU has
no process and is below the threshold.

## Fan-control troubleshooting

An unavailable control is shown directly on the GPU card and in
`fan_control_error` from `/health`. Common causes are a GPU without controllable
fans, a driver that does not export the v2 APIs, or insufficient permissions.
This implementation calls NVML directly and does not require Xorg, Coolbits,
`DISPLAY`, `XAUTHORITY`, or `nvidia-settings`.

After installing, verify on one idle-safe GPU from the dashboard or with the
API. Confirm the reported fan percentage changes, switch back to automatic,
and check the service log:

```bash
sudo journalctl -u simple-node-sentinel.service -f
curl -s http://127.0.0.1:8080/health
curl -s http://127.0.0.1:8080/api/gpus
```

## Tests

Tests use mocks for NVML fan writes, concurrency, restart/idle policy, SMTP and
process edge cases. SQLite tests use only an automatically removed temporary
directory and never change real fan settings:

```bash
conda run -n test python -m unittest discover -s tests -v
```

Production checks that require an administrator and real hardware should cover
cross-user `/proc` visibility, sensor labels, NVML values and fan writes, idle
automatic restoration, SMTP delivery and the final systemd sandbox.

# Simple Node Sentinel

A small, read-only monitor for multi-user Ubuntu NVIDIA GPU servers. It uses
FastAPI, psutil, NVML and SQLite, with a dependency-free browser interface.
It never kills processes, controls hardware, or executes commands received
from users.

## What it monitors

- CPU usage, per-core usage, load average, uptime and available temperatures
- Memory and swap
- Physical disk mounts (virtual filesystems are ignored)
- NVIDIA GPU utilization, memory, temperature, fan, power and compute processes
- GPU process owner, sanitized command, runtime, CPU, RAM and GPU memory
- Per-user process, CPU, RAM and GPU totals
- Sustained GPU temperature alerts and optional SMTP delivery

The latest snapshot stays in memory for fast live updates. SQLite also stores
system, GPU and disk metric history, GPU process lifetimes, temperature alert
events and email delivery records. Metric history is retained for three days by
default and is downsampled by the API before it reaches the browser.

## Requirements

- Ubuntu with Python 3.10 or newer
- NVIDIA driver providing NVML
- Root for production, so processes belonging to other users can be inspected
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

Temperature alerts are sent after five continuous minutes above the configured
threshold, with at most one alert per GPU every two hours. Recovery is recorded
after five continuous minutes below the recovery threshold, but no recovery
email is sent and the two-hour cooldown is preserved.

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
use `PrivateDevices`, because NVML needs `/dev/nvidia*`.

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
24-hour and 3-day ranges. Charts are served locally and do not require internet
access.

## Read-only API

- `GET /api/summary`
- `GET /api/gpus`
- `GET /api/gpu-processes`
- `GET /api/users`
- `GET /api/disks`
- `GET /api/alerts`
- `GET /api/history?range_seconds=3600&max_points=720`
- `GET /health`

The history endpoint accepts 60–259200 seconds and returns at most 1000
downsampled points per series. There are no POST or control endpoints.

## Tests

Tests use mocks for NVML, SMTP and process edge cases. SQLite tests use only an
automatically removed temporary directory:

```bash
conda run -n test python -m unittest discover -s tests -v
```

Production checks that require an administrator and real hardware should cover
cross-user `/proc` visibility, sensor labels, NVML values, SMTP delivery and
the final systemd sandbox.

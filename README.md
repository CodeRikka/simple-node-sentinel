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
- Per-GPU temperature→fan curves stored in SQLite and edited in the dashboard

The latest snapshot stays in memory for fast live updates. SQLite also stores
system, GPU and disk metric history, GPU process lifetimes, temperature alert
events, email delivery records, synced user notification settings and per-GPU
fan profiles. Metric history is retained for three days by default and is
downsampled by the API before it reaches the browser.

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

To fill or replace the SMTP password using the interactive script, run it in
a terminal with sudo (the production password is owned by root with mode
`600`):

```bash
sudo /opt/simple-node-sentinel/venv/bin/python \
  /opt/simple-node-sentinel/scripts/configure_interactively.py \
  --config /etc/simple-node-sentinel/config.yaml
sudo systemctl restart simple-node-sentinel.service
```

Do not add `</dev/null` to this command: that is used for unattended code
updates and skips configuration. If testing a fix from a local checkout,
replace the script path with that checkout's `scripts/configure_interactively.py`;
the installed virtual environment can still provide Python and its dependencies.
The script explicitly asks before replacing an existing password. Input stays
hidden; Enter keeps the old value, and successful writes report the destination
and mode without displaying the password. It offers to enable email only once
a non-empty password is available. A saved password does not verify SMTP login
or delivery.

The password file must contain only the SMTP password or app password, with no
quotes. User emails, admin flags and process-end notification toggles are
stored in the database and edited under **User settings** in the dashboard.
Linux login users (`UID >= UID_MIN`, plus root) are synced automatically on
startup and every six hours; the dashboard can also sync on demand. Users
without an email are named in temperature-alert bodies when they are missing.

Older YAML keys (`users`, `email.admin_emails`,
`process_end_notifications.users`, and the old global fan thresholds) are
ignored for runtime configuration. On first start after an upgrade they are
imported once into the database when still present.

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

`fan_control` enables dashboard curve editing and NVML writes. Only the global
feature switch and UI/write step size remain in YAML:

```yaml
fan_control:
  enabled: true
  step_percent: 5
```

Each GPU gets its own persisted profile the first time it is observed: min/max
fan percents, a cool-down hold (20 seconds by default), and a default curve
point of `80°C → 80%`. Temperature reaching a threshold immediately raises the
fan to that step, skipping steps when necessary. To drop one step, temperature
must stay at least 3°C below the current step's threshold for the hold time.
A rebound or missing temperature reading resets the timer. The lowest step
returns to NVIDIA automatic control; running processes do not block cooling
down. For example, `70°C → 70%, 80°C → 80%` rises to 80% at 80°C and drops to
70% after 20 seconds at or below 77°C. Another hold at or below 67°C returns
to automatic. Deleting every point also selects automatic mode.

The separate idle-temperature gate (previously 60°C) is retired. Existing
curves and limits are preserved; the stored idle duration becomes the
cool-down hold. Older API clients may still send `idle_duration_seconds`;
`cooldown_seconds` takes precedence. `idle_temperature_celsius` is accepted
for compatibility but no longer controls fan behavior.

Fan and user settings have no separate login. Everyone who can reach the
dashboard can change them. Keep the service bound to localhost and grant SSH
access only to trusted users. Set `fan_control.enabled: false` to retain a
read-only deployment for fans.

Temperature alerts are sent after five continuous minutes above the configured
threshold, with at most one alert per GPU every two hours. Recipients are
active users marked `is_admin`, plus GPU occupants who have
`notify_temperature` and an email. Recovery is recorded after five continuous
minutes below the recovery threshold, but no recovery email is sent and the
two-hour cooldown is preserved. On service startup, persisted active alerts are
compared with the first available GPU temperature sample; alerts already below
the recovery threshold are immediately marked recovered. Recovered records are
immutable and are never changed back to active—a later high-temperature event
creates a new alert.

Process-end emails go only to users with `notify_process_end` enabled. The
process must have run for at least `min_runtime_seconds` (default five minutes),
then be absent from NVML for `missing_duration_seconds` (default 20 seconds).
Short-lived processes are ignored.

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

### Updating a running installation

The service runs the copy in `/opt/simple-node-sentinel`, not your development
checkout. To deploy local changes, run the installer from that checkout;
uncommitted files are included, so a GitHub push or pull is not required.
From an account with sudo access, replace `/absolute/path/to/checkout` below
with the checkout containing the changes:

```bash
sudo -v
sudo systemctl stop simple-node-sentinel.service && \
  sudo bash /absolute/path/to/checkout/scripts/install.sh </dev/null && \
  sudo systemctl start simple-node-sentinel.service

sudo systemctl status simple-node-sentinel.service --no-pager -l
curl --fail --silent --show-error http://127.0.0.1:8080/health
```

Stopping first prevents serving a mixture of old and new files while copying.
The installer keeps the existing config, SMTP password, database, user settings
and fan profiles. Redirecting stdin from `/dev/null` skips configuration
prompts. Installation does not start or restart the service by itself;
the chained start above runs only after installation succeeds. If installation
fails, inspect the error, fix it and rerun the update sequence.

Allow a few seconds after starting for the health endpoint to become available.
For startup errors, inspect:

```bash
sudo journalctl -u simple-node-sentinel.service -n 80 --no-pager
```

Then hard-refresh the dashboard (`Ctrl+Shift+R`, or `Cmd+Shift+R` on macOS)
to load the updated JavaScript and CSS. This briefly interrupts monitoring
and the dashboard; it does not stop GPU workload processes.

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
24-hour and 3-day ranges. Each GPU card edits its own fan curve with a live
preview. Workload users occupy a scrollable strip so multiple users do not
resize the metric grid. GPU cards stay in two columns above 760px; charts
inside narrower cards stack vertically. Hover a history line, its legend, or
a corresponding GPU metric to fade the other series. Legend buttons also
support keyboard focus and click-to-pin highlighting; Escape clears it.
Fan previews use one step line with separate dashed cooling thresholds.

GPU process rows show a compact command preview. **View** opens the complete
command in a separate reading dialog with a copy button, without expanding
the table row. The dialog stays stable while metrics refresh; sensitive
arguments remain redacted. The header links to this GitHub
repository. The theme switch selects light or dark mode; light is the default,
and the browser remembers your choice independently of the system theme.
The animated fan icon is green at 60°C and below, transitions toward
red between 60°C and 90°C, and stays red at 90°C and above. Charts and icons
are served locally and do not require internet access.

## API

- `GET /api/summary`
- `GET /api/gpus`
- `GET /api/gpu-processes`
- `GET /api/users`
- `GET /api/settings/users`
- `POST /api/settings/users/sync`
- `PATCH /api/settings/users/{username}`
- `GET /api/disks`
- `GET /api/alerts`
- `GET /api/history?range_seconds=3600&max_points=720`
- `GET /health`
- `PUT /api/gpus/{gpu_uuid}/fan-profile`

The history endpoint accepts 60–259200 seconds and returns at most 1000
downsampled points per series. `GET /api/gpus` includes a `fan_control` object
with profile limits, `curve_points`, `mode` (`auto` when empty, otherwise
`curve`), `applied_percent`, `revision`, capability and error fields.

Use the latest `revision` when saving a fan profile. For example:

```bash
curl -X PUT http://127.0.0.1:8080/api/gpus/GPU-UUID/fan-profile \
  -H 'Content-Type: application/json' \
  -d '{
    "minimum_percent": 30,
    "maximum_percent": 100,
    "cooldown_seconds": 20,
    "curve_points": [{"temperature_celsius": 80, "fan_percent": 80}],
    "expected_revision": 0
  }'
```

Fan profile writes are serialized. If two users submit from the same revision,
the first successful request wins and the other receives HTTP 409 with the
newest state. An empty `curve_points` list switches the GPU to NVIDIA automatic
mode.

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

Tests use mocks for NVML fan writes, concurrency, restart/cool-down policy, SMTP and
process edge cases. SQLite tests use only an automatically removed temporary
directory and never change real fan settings:

```bash
conda run -n test python -m unittest discover -s tests -v
```

Production checks that require an administrator and real hardware should cover
cross-user `/proc` visibility, sensor labels, NVML values and fan writes, step-down
automatic restoration, SMTP delivery and the final systemd sandbox.

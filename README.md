# lg-airco-logger

A raw logger for an LG ThinQ air conditioner, through LG's official
**ThinQ Connect API**: the same calls as LG's own SDK
([thinq-connect/pythinqconnect](https://github.com/thinq-connect/pythinqconnect))
and Home Assistant's `lg_thinq` integration. It reads, it never controls.

Every answer from LG is stored unchanged, one JSON line per request, so the
data can be interpreted again later. The logs are copied to a NAS with
`rclone copy` (which only adds and updates, never deletes). Python standard
library only; runs fine on a Raspberry Pi 1.

## What it logs

| When | Request | `kind` |
|---|---|---|
| every 30 s | `GET devices/<id>/state`: on/off, mode, measured and target temperature, fan speed, vanes, power save, filter, timers | `state` |
| every 30 s | `GET devices/energy/<id>/usage?…period=DAILY` for yesterday and today: LG keeps today's total up to date during the day (every 5 minutes so far), so the differences give the energy per update; asking every 30 s shows at once if LG ever updates more often | `energy_today` |
| at start, then daily at 08:30 | `GET devices`, `devices/<id>/profile`, `devices/energy/<id>/profile`, `route` | `devices`, `profile`, `energy_profile`, `route` |
| at start, then daily at 08:30 | `GET devices/energy/<id>/usage?…period=DAILY`: the energy per day in Wh; at start every month LG still keeps, then the last 8 days | `energy` |
| a request that fails | the status and LG's answer, or the reason | `error` |

Files: `<logdir>/YYYY/YYYYMMDD.jsonl`, by UTC date, one line per request:

```json
{"t":"2026-11-06T07:55:35.123Z","kind":"state","path":"devices/<id>/state","http":200,"ms":674,"body":{"messageId":"…","timestamp":"…","response":{"temperature":{"currentTemperature":21.5,"targetTemperature":22,"unit":"C"},"airFlow":{"windStrength":"AUTO"},…}}}
```

The token is never logged. The device id is (it is not a secret, but it is
specific to one home: keep the logs out of public places).

## What LG's API does and does not give (checked 2026-11-06)

- Energy per day and per month only, in Wh; no hours, and no current power
  (the LG app shows those through LG's internal app interface, which is not
  part of this API). But today's figure is already there during the day, and
  LG updates it every 5 minutes (checked 2026-11-06 with requests every
  minute: a new total at about :x0:30 and :x5:30 UTC, unchanged in between;
  +54 Wh each while the meters showed the airco at about 0.65 kW), so the
  differences give the energy per 5 minutes after all.
- About 12 months of energy history; older months are refused (`1102
  Unacceptable parameters`). The daily energy request keeps the history from
  being lost. LG documents the limits in its public API reference
  ([ThinQ Connect](https://smartsolution.developer.lge.com/en/apiManage/thinq_connect),
  "Retrieve Energy Data"): `DAILY` up to 31 days per request, `MONTHLY` up to
  12 months. The same page says personal access tokens are for personal,
  non-commercial use.
- The measured temperature comes in steps of 0.5 °C.
- How often you may ask is not documented. A test of 76 status requests,
  every 30, then 10, then 5 seconds, was answered without a single error
  (0.6–0.9 s each). The logger asks for the status and today's energy every
  30 s (5,760 requests a day) and on any status error waits twice as long
  each time, up to 30 minutes.

## The token

Make a Personal Access Token at https://connect-pat.lgthinq.com with the LG
ThinQ account. Read-only scopes are enough, and safer:

| Scope | Needed |
|---|---|
| View devices | yes |
| View device statuses | yes |
| Energy consumption inquiry | yes |
| Device control | **no**: it lets the token operate the airco |
| Event subscription, push notifications | not used (yet) |

## Install

On the Pi, from a checkout:

```sh
sudo ./install.sh
```

Then put the secrets in place (never in a repo):

```sh
# the token: one line, root only (systemd hands it to the service)
sudo sh -c 'read -rs T && printf "%s\n" "$T" > /etc/lg-airco-logger/token && unset T'
sudo chmod 600 /etc/lg-airco-logger/token

# the NAS: an rclone remote (here reusing another logger's), and where to copy to
sudo install -m 640 -g lg-airco /etc/housemon/rclone.conf /etc/lg-airco-logger/rclone.conf
echo 'REMOTE=nas:<share>/<host>/lg-airco' | sudo tee /etc/lg-airco-logger/sync.env
sudo chgrp lg-airco /etc/lg-airco-logger/sync.env

sudo systemctl enable --now lg-airco-logger.service lg-airco-sync.timer
```

Check: `journalctl -u lg-airco-logger -n 20`, and the newest lines with
`sudo tail -n 2 /var/lib/lg-airco-logger/logs/$(date -u +%Y)/$(date -u +%Y%m%d).jsonl`.
After an update: `sudo ./install.sh && sudo systemctl restart lg-airco-logger`.

## Compact extract for a live view (optional)

`lg-airco-compact.py` turns the raw logs into a small file per UTC day,
`/var/lib/lg-airco-logger/compact/YYYY/YYYYMMDD.csv`: only what changed, a
few hundred lines (about 10 KB) a day instead of LG's full answers.

```text
2026-11-06T16:13:17Z,state,POWER_ON,HEAT,HIGH,25.5,30   operation, mode, fan, airco's own reading, target (°C)
2026-11-06T16:13:16Z,energy,20261106,9591               LG's date, LG's total for that day so far (Wh)
```

A `state` line comes when something changes, at least every 15 minutes, and
as the first line of each file (with the last energy totals). Every run
continues where the last one stopped, so it is cheap enough to run every
minute on a Pi 1; `--rebuild` makes everything again from the raw logs, which
stay the source.

`lg-airco-push.timer` runs it every minute and copies the compact folder with
`rsync` to another machine, e.g. for [housemon-lite](https://github.com/kduvekot/housemon-lite).
The receiving side decides where the files may go: its `authorized_keys`
allows this key only `rrsync` into one folder.

```sh
# on the logger (needs rsync): a key of its own, and where to push to
sudo apt install rsync
sudo ssh-keygen -t ed25519 -N '' -C lg-airco-push -f /etc/lg-airco-logger/push_key
sudo chmod 600 /etc/lg-airco-logger/push_key
echo 'PUSH_TARGET=<user>@<host>' | sudo tee /etc/lg-airco-logger/push.env
sudo sh -c 'ssh-keyscan -t ed25519 <host> > /etc/lg-airco-logger/push_known_hosts'

# on the receiving machine (needs rsync): a user that may only write one folder
sudo apt install rsync
sudo useradd --system --home-dir /var/lib/<user> --create-home --shell /bin/sh <user>
sudo install -d -m 755 -o <user> -g <user> <folder>
# in /var/lib/<user>/.ssh/authorized_keys (mode 600, owned by <user>), one line:
#   restrict,command="/usr/bin/rrsync -wo <folder>" ssh-ed25519 AAAA… lg-airco-push

# back on the logger
sudo systemctl enable --now lg-airco-push.timer
```

## Files

| File | |
|---|---|
| `lg-airco-logger.py` | the logger |
| `systemd/lg-airco-logger.service` | runs it as user `lg-airco`, the token through `LoadCredential` |
| `systemd/lg-airco-sync.service`, `.timer` | `rclone copy` of the logs to the NAS every 5 minutes |
| `lg-airco-compact.py` | the compact extract (see above) |
| `systemd/lg-airco-push.service`, `.timer` | the extract plus `rsync` to a live view, every minute (optional) |
| `install.sh` | user, folders, files and units; leaves the secrets alone |

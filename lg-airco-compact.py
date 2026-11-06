#!/usr/bin/env python3
"""Compact extract of lg-airco-logger's raw logs, for live views such as
housemon-lite: only what changed, a few hundred lines a day instead of LG's
full answers. The raw logs stay the source; this can always be made again
from them (--rebuild).

Files: <outdir>/YYYY/YYYYMMDD.csv, by UTC date like the raw logs. Lines:

  2026-11-06T16:13:17Z,state,POWER_ON,HEAT,HIGH,25.5,30
      time, "state", operation, job mode, fan, airco's own reading (°C), target (°C):
      written when one of them changes, at least every 15 minutes (so a reader
      knows the logger is alive), and as the first line of every file
  2026-11-06T16:13:16Z,energy,20261106,9591
      time, "energy", LG's date (YYYYMMDD), LG's total for that day so far (Wh):
      written when the total of a day changes (LG updates it every 5 minutes)

The times are when the logger asked (UTC). Every run continues where the
previous one stopped (<outdir>/.state.json); a run takes a fraction of a
second, so it can run every minute on a Raspberry Pi 1. Standard library only.
"""
import argparse
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HEARTBEAT = timedelta(minutes=15)


def raw_files(logdir):
    return sorted(Path(logdir).glob("[0-9][0-9][0-9][0-9]/[0-9]*.jsonl"))


class Compactor:
    def __init__(self, outdir):
        self.outdir = Path(outdir)
        self.state_file = self.outdir / ".state.json"
        try:
            s = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            s = {}
        self.raw = s.get("raw")                 # raw file being read, and how far
        self.offset = s.get("offset", 0)
        self.last_state = s.get("state")        # [operation, mode, fan, reading, target]
        self.last_state_t = s.get("state_t")
        self.energy = s.get("energy", {})       # LG date -> Wh last written
        self.out_day = s.get("out_day")         # UTC day of the compact file last written
        self.lines = {}                         # compact file -> new lines

    def save(self):
        for path, lines in self.lines.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="ascii") as f:
                f.write("".join(lines))
        self.lines = {}
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"raw": self.raw, "offset": self.offset, "state": self.last_state,
                                   "state_t": self.last_state_t, "energy": self.energy, "out_day": self.out_day}))
        tmp.replace(self.state_file)

    def emit(self, t, fields):
        day = t[:10].replace("-", "")
        path = self.outdir / day[:4] / f"{day}.csv"
        self.lines.setdefault(path, []).append(",".join([t] + [str(x) for x in fields]) + "\n")

    def record(self, r):
        if r.get("http") != 200 or "body" not in r:
            return
        t = r["t"][:19] + "Z"
        day = t[:10]
        if day != self.out_day:                 # a new file starts with the full picture
            self.out_day = day
            if self.last_state:
                self.emit(t, ["state"] + self.last_state)
                self.last_state_t = t
            for d in sorted(self.energy)[-2:]:
                self.emit(t, ["energy", d, self.energy[d]])
        resp = r["body"].get("response") or {}
        if r["kind"] == "state":
            st = [resp.get("operation", {}).get("airConOperationMode", ""),
                  resp.get("airConJobMode", {}).get("currentJobMode", ""),
                  resp.get("airFlow", {}).get("windStrength", ""),
                  resp.get("temperature", {}).get("currentTemperature", ""),
                  resp.get("temperature", {}).get("targetTemperature", "")]
            old = datetime.fromisoformat(self.last_state_t[:19]) if self.last_state_t else None
            if st != self.last_state or old is None or datetime.fromisoformat(t[:19]) - old >= HEARTBEAT:
                self.emit(t, ["state"] + st)
                self.last_state, self.last_state_t = st, t
        elif r["kind"] == "energy_today":      # not "energy": the daily 12-month backfill
            for x in (resp.get("result") or {}).get("dataList") or []:
                d, wh = x.get("usedDate"), x.get("energyUsage")
                if d and wh is not None and self.energy.get(d) != wh:
                    self.energy[d] = wh
                    self.emit(t, ["energy", d, wh])
            for d in sorted(self.energy)[:-3]:  # only the last days are needed
                del self.energy[d]

    def run(self, logdir):
        files = [str(p) for p in raw_files(logdir)]
        if self.raw in files:
            todo = files[files.index(self.raw):]
        else:
            todo, self.offset = files[-1:], 0   # first run: start with today
        for path in todo:
            if path != self.raw:
                self.raw, self.offset = path, 0
            with open(path, "rb") as f:
                f.seek(self.offset)
                for line in f:
                    if not line.endswith(b"\n"):
                        break                   # a line still being written: next run
                    self.offset += len(line)
                    try:
                        self.record(json.loads(line))
                    except (ValueError, KeyError, TypeError, AttributeError):
                        pass                    # a damaged line: skip it
        self.save()


def main():
    ap = argparse.ArgumentParser(description="compact extract of lg-airco-logger's raw logs")
    ap.add_argument("--logdir", required=True, help="the logger's raw logs (YYYY/YYYYMMDD.jsonl)")
    ap.add_argument("--outdir", required=True, help="where the compact files go")
    ap.add_argument("--rebuild", action="store_true", help="remove the compact files and make them again from all raw logs")
    a = ap.parse_args()
    out = Path(a.outdir)
    if a.rebuild and out.exists():
        for p in out.iterdir():
            shutil.rmtree(p) if p.is_dir() else p.unlink()
    out.mkdir(parents=True, exist_ok=True)
    c = Compactor(out)
    if a.rebuild:
        c.raw = None
        files = raw_files(a.logdir)
        if files:
            c.raw, c.offset = str(files[0]), 0
    c.run(a.logdir)


if __name__ == "__main__":
    sys.exit(main())

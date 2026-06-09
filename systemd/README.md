# systemd user units

Four long-running daemons plus a daily-digest timer. Install per-user (no sudo)
so they live alongside the user's `~/.engram/` runtime.

```bash
mkdir -p ~/.config/systemd/user
cp /data/projects/engram/systemd/engram-*.service ~/.config/systemd/user/
cp /data/projects/engram/systemd/engram-daily-digest.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now \
  engram-projector engram-watcher engram-reactor engram-poller engram-daily-digest.timer
```

Check:
```bash
systemctl --user status engram-projector
journalctl --user -u engram-reactor -f
```

The units assume:
- venv at `~/.engram/.venv` with `engram` installed — `./bin/eos-init` builds it
- config at `~/.engram/config.yml`

If the venv lives elsewhere, edit the `ExecStart=` paths.

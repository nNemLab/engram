# systemd user units

Three long-running daemons. Install per-user (no sudo) so they live alongside
the user's `~/.engram/` runtime.

```bash
mkdir -p ~/.config/systemd/user
cp /data/projects/agenticOS/engram/systemd/engram-*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now engram-projector engram-watcher engram-reactor
```

Check:
```bash
systemctl --user status engram-projector
journalctl --user -u engram-reactor -f
```

The units assume:
- venv at `~/.engram/.venv` with `engram` installed (editable or wheel)
- config at `~/.engram/config.yml`

If the venv lives elsewhere, edit `ExecStart=` paths.

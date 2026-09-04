# agents-relay

Thin relay over [agents-harness](https://github.com/Lolaplex/agents-harness) `runner.loop`: one subprocess per turn, trailer parsing for session metadata. No identity store, no traces.

```bash
pip install -e .
export RELAY_SECRET=dev
python -m agents_relay serve
```

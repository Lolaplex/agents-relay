# agents-gateway

Thin gateway over [agents-harness](https://github.com/Lolaplex/agents-harness) `runner.loop`: one subprocess per turn, trailer parsing for session metadata. No identity store, no traces.

```bash
pip install -e .
export GATEWAY_SECRET=dev
python -m agents_gateway serve
```

# Scenario-battery runs (S1–S8 + rubrics)

The S1–S8 battery (`weekend-window/app/scenarios.py`) rides on **live model behavior and an LLM judge**, so it is
inherently nondeterministic — a green run is a *sample*, not a gate. (The deterministic gate is the seeded suite,
enforced by CI on every push.) Each battery run is logged here with its date and agent version; rubric scores
(R1 conversational · R2 update quality · R3 memory-in-use · R4 etiquette, 1–5, gate ≥ 4) vary run to run.

| Date | Agent version | Architecture under test | S1–S8 | Rubrics R1–R4 | Notes |
|---|---|---|---|---|---|
| 2026-07-01 | v1 | initial CMA port (session-per-channel, shared store) | 17/18 | — | the one failure was a **harness assertion bug**, not agent behavior: S5's check required the word "storm" absent from the all-clear ping, but the model correctly wrote "storms gone, clear and pleasant". Assertion fixed to check the positive signal. |
| 2026-07-01 | v1 | same, assertion fixed | 18/18 | 5 / 5 / 5 / 4 | R4 deduction: judge flagged the three first-outlook "all green" pings as borderline chatty (they are by design — SPEC posts a first outlook per watch). |
| 2026-07-01 | v6 | per-channel memory stores; four tools (get_forecast/list_monitors added); context catch-up pull | 18/18 | 5 / 5 / 5 / 5 | lazy store creation verified live (C_SCEN got its own store); S7 recall via the channel's own store. |

To add a run: `python scenarios.py` (needs `ANTHROPIC_API_KEY`; ~13 live turns), then append a row.

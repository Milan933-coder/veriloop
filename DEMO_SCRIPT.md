# Demo script (~6 minutes)

## Before going on stage (T-30 min)
1. `cd backend && python -m veriloop`. Wait for the `prewarm_finished` log line, so the ML models are loaded.
2. Open `http://localhost:8000`. The footer badge should show both resolved models (`openai/gpt-6-luna`,
   `openai/gpt-5.6-luna`) and the tool status.
3. Re-run the queries in `docs/demo_cases.md`; news counts drift, so confirm the sources still surface.
4. **Backup (network-proof):** record cassettes once with `TOOL_MODE=record python -m veriloop` +
   `python scripts/record_demo.py`. If the venue network fails, restart with `TOOL_MODE=replay`. The UI
   shows **REPLAY MODE**. Keep the pre-recorded backup video on the laptop and on a USB stick.
5. reporter-agent: log in once to the demo X account in its persistent profile
   (`python reporter-agent/reporter_agent.py --help`).

## On stage

**0:00 – The problem (20 s).** "Fact-checkers stop at a label. Victims of deepfakes still have to report,
escalate and prove what happened. VeriLoop closes that loop, and it never guesses."

**0:20 – Case (a) TRUE.**
* Paste the DA-hike claim and point at the **live trace**: three branches run in parallel.
* The verdict is **TRUE** with ≥ 2 independent citations, including PIB (government-source badge).
* Show the calibrated confidence meter.

**1:30 – Case (b) FALSE + report.**
* Upload the Cyclone Arnab clip (`D:/veriloop/demo/case_b/demo_case_b.mp4`) or paste the post URL.
* Provenance: a **known-fake match** (pHash/CLIP) to July 2026 Typhoon Bavi footage, plus fact-checks.
* Verdict **FALSE**. Click **Prepare report**, and the app shows exactly what will be sent: bundle,
  report card, text.
* Click **I confirm**. The reporter-agent opens X's report dialog pre-filled and **stops; a human presses Submit**.

**3:00 – Case (c) MANIPULATED + legal.**
* Upload the Blumenthal clip. Show the per-model forensic bars and the abstain band, the fact-check
  matches, and the verdict **MANIPULATED**.
* Open the legal wizard and pick India. Tick the 4 acknowledgements, and an IT Rules 2021 grievance
  draft appears with placeholders.
* Point out: **no send button anywhere.**

**4:15 – Case (d) Abstain.** Paste the Pay Commission claim. The result is **UNVERIFIABLE** with its reason.
"Unsure means we say so. Grey means *no evidence*, which is not the same as *false*."

**4:45 – Outage.** Break GDELT live:
```bash
curl -X POST localhost:8000/admin/chaos -H "X-Admin-Key: $ADMIN_KEY" -H "Content-Type: application/json" \
     -d '{"tool":"gdelt","mode":"error"}'
```
Re-run case (a). The result is degraded and names the failed tool, and nothing crashes. Then switch chaos `off`.

**5:15 – Tamper-evident audit.**
1. Open `/a/{job_id}` for case (b) and click **Verify chain**: ✓ `ok:true`. The browser also re-checks the
   SHA-256 and the Ed25519 signature itself.
2. In a terminal run `python scripts/tamper_demo.py --job <job_id>`, then **Verify** again: ✗ `broken_at: N`.
3. Run `python scripts/restore_chain.py`, then **Verify**: ✓. Download the signed PDF; its QR code points back here.

**5:45 – Close.** "Detection → evidence → report → legal → audit. Works with zero paid keys; every paid API
plugs in when its key appears."

## If something goes wrong
| Symptom | Action |
|---|---|
| Venue network down | Restart with `TOOL_MODE=replay` (pre-recorded cassettes) or play the backup video |
| A source rate-limits | The UI shows "processing (rate-limited: …)". Keep talking; it resumes |
| Model provider down | Startup fails fast with `model_resolution_failed`; use replay mode |
| Reporter selectors changed | The agent prints the deep link + written steps: open them manually |

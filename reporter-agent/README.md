# reporter-agent

Presenter-side helper for VeriLoop's platform reports (D-10). After a person reviews a report package
in the web app and clicks **I confirm**, the API returns a command like:

```bash
python reporter-agent/reporter_agent.py --api http://localhost:8000 --job <job_id> --attempt <attempt_id>
```

The agent then:

1. fetches `GET /report/{job_id}/attempts/{attempt_id}` (only **confirmed** attempts exist there),
2. opens the post in a **headed** Chromium with a persistent profile, so you log in to X / Instagram /
   Facebook / YouTube once and stay logged in (default `VERILOOP_HOME/reporter-profile`, i.e.
   `D:/veriloop/reporter-profile`; override with `--profile` or `VERILOOP_REPORTER_PROFILE`),
3. opens the post's report dialog using `selectors/<platform>.json` and pastes the confirmed text where
   the dialog has a text box,
4. highlights the final **Submit** button and **STOPS**. You choose the category, review, and press
   Submit yourself. **It never submits** (SPEC D4 / G5).

If a selector does not match (platforms change their markup often) or the platform has no selectors,
it prints the deep link, the step-by-step instructions and the text to paste instead. Telegram /
WhatsApp plans (`report_card`) and unknown sites (`deeplink_guided`) are always manual.

## Options

| flag | default | meaning |
|---|---|---|
| `--api` | `$VERILOOP_API` or `http://localhost:8000` | VeriLoop server |
| `--job`, `--attempt` | required | from the confirm response |
| `--api-key` | `$VERILOOP_API_KEY` | only when the server sets `API_KEYS` |
| `--profile` | `VERILOOP_HOME/reporter-profile` | persistent browser profile (keep it off C:) |
| `--dry-run` | off | print the plan and selectors, no browser |

Uses the shared VeriLoop venv (`playwright` + its Chromium are already installed there):
`C:/Users/lokar/.veriloop/venv/Scripts/python.exe reporter-agent/reporter_agent.py ...`

## Selector files

`selectors/<platform>.json`: `steps` (`click` / `fill` / `wait`, each with alternative CSS/Playwright
selectors tried in order, `optional` steps may fail), `final_submit` (highlighted only) and `notes`.
`agent_core.validate_selectors` refuses a file whose `click` step mentions submit/send or reuses a
`final_submit` selector, so a bad edit cannot make the agent submit. Selectors are best effort as of
2026-10-31 and were not exercised against logged-in accounts during the build.

## Tests

```bash
C:/Users/lokar/.veriloop/venv/Scripts/python.exe -m pytest reporter-agent/tests -q -p no:cacheprovider
```

They cover the non-browser parts: package fetch (mocked HTTP), the confirmed-only gate, selector-file
safety, manual fallback text, profile location and the CLI (dry run, card plans, errors).

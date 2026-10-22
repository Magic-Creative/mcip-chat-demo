# Contributing

Thanks for improving the demo. Keep changes small and in the spirit of the
project: this code exists to be *read* by integrators, so clarity beats
cleverness and every non-obvious behaviour should match a section of
`docs/integration-guide.md` (quote it in a comment when it helps).

## Development loop

```bash
uv sync                 # create .venv with the exact pins
cp .env.example .env    # fill in DEMO_ENCRYPTION_KEY and DEMO_SESSION_SECRET
                        # (MCIP_BASE_URL is an optional first-run default;
                        #  the MCip address lives in the GUI: Settings)
uv run uvicorn app.main:app --reload --port 8090
```

The UI is plain HTML/JS in `static/` — edit and reload the page, no build step.

## Before a pull request

```bash
uv run ruff check .          # lint (line length 100, py312)
uv run pytest                # unit tests; the suite fakes MCip, no network
uv run pytest -m smoke       # optional: one real turn against a live
                             # deployment (needs MCIP_SMOKE_* env vars, see README)
```

Guidelines:

* Python 3.12, typed function signatures, no new runtime dependencies without
  a reason recorded in the PR.
* Never log, return, or template a full API key — only `key_prefix()` values.
* New error handling must go through `app/errors.py` (`ui` state + advice);
  the frontend switches on `ui`, never on message text.
* Tests must not require a network or a real MCip deployment.

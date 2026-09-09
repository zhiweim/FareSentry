# FareSentry

A personalized flight-shopping agent for the Agents for Humans hackathon.
This initial scaffold contains domain models and a flight-provider contract.
Live flight retrieval, the Strands agent, persistence, frontend, notifications,
and AWS infrastructure are not implemented yet.

## Local setup (Windows PowerShell)

Install Python 3.13 and run these commands from the repository directory:

```powershell
py -3.13 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -e '.[dev]'
```

These commands use the virtual environment directly, so activation and execution
policy changes are unnecessary. If `.venv` already exists, verify that it works
and uses Python 3.13 before reusing it. Rename an incompatible environment before
creating a new one. If `py -3.13` is unavailable, install Python 3.13 with the
Windows Python launcher, or invoke your Python 3.13 executable by its full path.
The existing `.python-version` also selects 3.13.0 for pyenv users.

Runtime dependencies are `strands-agents`, `pydantic`, `requests`, and
`python-dotenv`. The `dev` extra installs `pytest` and `ruff`.

## Checks

```powershell
& .\.venv\Scripts\python.exe -m pytest
& .\.venv\Scripts\python.exe -m ruff check .
& .\.venv\Scripts\python.exe -m ruff format --check .
```

Tests are offline and require no credentials.

## Structure

```text
pyproject.toml                 Dependencies, packaging, pytest and Ruff settings
src/faresentry/
    __init__.py
    models.py                 TripQuery and FlightOption Pydantic models
    providers/
        __init__.py
        base.py               FlightProvider protocol: search(query)
        serpapi.py            SerpApiFlightProvider placeholder
tests/
    test_models.py            Domain validation tests
    test_providers.py         Explicit placeholder behavior
```

`TripQuery` accepts one origin, destination, outbound date, return date, and
currency. Airport and currency codes must be three uppercase letters; this is
format validation, not a lookup of valid codes. Return dates cannot precede
outbound dates, and the airports must differ.

`FlightOption` contains airline, a nonnegative decimal price, currency,
nonnegative stop count, and positive duration in minutes (including layovers).
It is a minimal summary: route/segment details and provider response
normalization will be added with live retrieval. Providers must compare options
with the same journey scope and currency.

`FlightProvider.search()` returns a list of normalized `FlightOption` objects.
The SerpApi placeholder raises `NotImplementedError`; it does not make requests.

## Manual configuration

No API keys or AWS configuration are needed for this scaffold. When live search
is implemented, read `SERPAPI_API_KEY` from the environment. A local `.env` file
is ignored by Git, but nothing loads it yet. Future Bedrock integration should
use standard AWS credentials. Never commit credentials.

Python handles calculations and hard constraints; the future Strands agent will
reason about tradeoffs and explanations.

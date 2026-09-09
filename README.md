# FareSentry

A personalized flight-shopping agent for the Agents for Humans hackathon.
MVP-001 retrieves real outbound flight choices from SerpApi Google Flights and
normalizes them into domain models. Return-flight selection, the Strands agent,
persistence, frontend, notifications, and AWS infrastructure are not implemented.

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

Tests use synthetic responses and mocks, require no credentials, and block
unmocked Requests calls. They never make real SerpApi requests.

## Structure

```text
pyproject.toml                 Dependencies, packaging, pytest and Ruff settings
src/faresentry/
    __init__.py
    models.py                 TripQuery and FlightOption Pydantic models
    providers/
        __init__.py
        base.py               FlightProvider protocol: search(query)
        serpapi.py            SerpApi retrieval and defensive normalization
scripts/
    search_flights.py         Manual real-data smoke test
tests/
    conftest.py               Block real HTTP requests during unit tests
    test_models.py            Domain validation tests
    test_providers.py         Mocked request, parsing, and error tests
    test_search_flights.py    Smoke-test CLI validation and output tests
```

`TripQuery` accepts one origin, destination, outbound date, return date, and
currency. Airport and currency codes must be three uppercase letters; this is
format validation, not a lookup of valid codes. Return dates cannot precede
outbound dates, and the airports must differ.

`FlightOption` represents an **outbound choice with a round-trip quoted price**.
It does not contain a selected return itinerary. Route, flight numbers, duration,
stops, and layover metrics describe only the outbound journey. This matches the
[initial Google Flights response](https://serpapi.com/google-flights-api): its
`departure_token` can later retrieve compatible return choices. This milestone
preserves that token but never uses it to fetch returns.

The existing `airline` field remains a display string (multiple airlines joined
with ` / `). Added fields are `airlines`, `flight_numbers`, `origin`,
`destination`, `max_layover_minutes`, and `departure_token`, with defaults that
keep existing callers compatible. Prices use `Decimal`; total duration comes
from SerpApi's `total_duration`, including layovers. Python calculates stops as
the outbound segment count minus one, and maximum layover from the supplied
layover durations. Nonstop options have a maximum layover of zero; incomplete
connecting-flight layover data is `None`, not zero. Missing optional text is
unknown or an empty list; unavailable airline names display as `Unknown airline`.

`FlightProvider.search()` returns only normalized `FlightOption` objects,
combining `best_flights` followed by `other_flights`. Missing result groups and
zero results produce an empty list. Unusable individual results (for example,
missing price or duration, or malformed segments) are skipped independently.
Missing optional metadata is retained as unknown where possible. If every result
is malformed, the returned list is empty. No raw API dictionaries are returned.

The provider reads `SERPAPI_API_KEY` from the environment at search time. Requests
use round trip (`type=1`), economy (`travel_class=1`), USD, English, and US locale.
Non-USD queries are rejected explicitly. Each call uses a 30-second Requests
timeout and no automatic retries or redirects. Missing credentials, request
failures, non-2xx responses, invalid JSON, and SerpApi error responses raise
`FlightProviderError` with a safe message; raw upstream errors and URLs are not
printed or logged.

Before sending a search, the provider installs a credential-redacting filter on
Requests and urllib3 transport loggers, including `urllib3.connectionpool` and
`urllib3.util.retry` (and legacy Requests-vendored equivalents). It redacts
`api_key` values and literal/URL-encoded copies of the current key in messages
and exception text before handlers receive the record. Logging levels and
application logging stay unchanged, and repeated searches reuse the filter.
The real outgoing query parameter is unchanged. Tests exercise the real
Requests/urllib3 logging path with a mocked connection and blocked sockets/DNS.

Redaction is an output safeguard, not memory isolation: the environment,
request URL, and debugger-visible locals still contain the key. Developer tools
that inspect those objects, dump process memory, or enable `http.client` raw
wire output bypass these logging filters. Keep such captures private; do not
print or log raw request objects through unrelated application loggers.

## SerpApi local setup and real-data smoke test

Get a key from your [SerpApi account](https://serpapi.com/manage-api-key). Create
or edit `.env` in the repository root using your editor, and add:

```dotenv
SERPAPI_API_KEY=your_actual_serpapi_key
```

`.env` is ignored by Git. Never place a real key in source code, documentation,
or `.env.example`, and never commit it. The smoke-test script loads this specific
`.env` file using `python-dotenv`; an existing environment variable takes
precedence. The provider itself does not load files or require AWS credentials.

After the editable installation above, run this from the repository root in
Windows PowerShell (this makes one live search and uses your SerpApi quota):

```powershell
.\.venv\Scripts\python.exe .\scripts\search_flights.py --origin LAX --destination HND --outbound 2026-10-12 --return-date 2026-10-27
```

Choose future dates when running this example later. The script prints outbound
airlines, round-trip USD price, route, duration, stops, maximum layover, flight
numbers, and whether a return lookup token is available. It never prints the
full token or raw JSON. A failed search prints a safe error and exits with code
1; invalid arguments exit with code 2. A successful search, including no usable
results, exits with code 0.

Python handles calculations and hard constraints; the future Strands agent will
reason about tradeoffs and explanations.

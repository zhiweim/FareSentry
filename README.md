# FareSentry

A personalized flight-shopping agent for the Agents for Humans hackathon.
MVP-001 retrieves real outbound flight choices from SerpApi Google Flights and
normalizes them into domain models. A selected outbound choice can now retrieve
compatible return options as completed round-trip itineraries. The Strands agent,
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
    models.py                 Trip queries, outbound choices, and typed itineraries
    providers/
        __init__.py
        base.py               FlightProvider protocol: search and return lookup
        serpapi.py            SerpApi retrieval and defensive normalization
scripts/
    search_flights.py         Manual real-data smoke test
tests/
    conftest.py               Block real HTTP requests during unit tests
    test_models.py            Domain validation tests
    test_itineraries.py       Segment, connection, and round-trip model tests
    test_providers.py         Mocked request, parsing, and error tests
    test_return_options.py    Return lookup, complete itineraries, and pricing tests
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
`departure_token` retrieves compatible return choices when the caller explicitly
selects an outbound option for return lookup.

Each `FlightOption` retains a required, immutable `outbound: FlightItinerary`,
with ordered `FlightSegment` objects and one explicit connection between each
pair. Same-airport connections are `Layover` objects; airport changes are
`AirportTransfer` objects. The provider uses adjacent segment airport IDs for
connection endpoints and the corresponding API layover duration for the entire
connection interval. It never infers missing durations from local timestamps.

Existing summary attributes remain readable: `airline` (names joined with
` / `), `airlines`, `flight_numbers`, `origin`, `destination`, `duration_minutes`,
`stops`, and `max_layover_minutes`. They are derived from `outbound`, including
airport transfers in duration and maximum connection time. Nonstop options have
zero stops and a maximum layover of zero. Price remains a `Decimal` round-trip
quote; `currency` and the repr-hidden `departure_token` stay on the search choice.
The token is workflow state and is absent from the itinerary itself.

Code constructing options must now pass `outbound` instead of summary fields;
summary-only input cannot reconstruct the individual flights and connections.
Serialized options contain `outbound`, `price`, `currency`, and `departure_token`,
without duplicated summary values. Treat serialized options as internal workflow
data because they include the lookup token. The CLI's readable output is unchanged.

`FlightProvider.search()` returns only normalized `FlightOption` objects,
combining `best_flights` followed by `other_flights`. Missing result groups and
zero results produce an empty list. Unusable individual results (for example,
missing price, segment airports, airline, flight number, or duration) are skipped
independently. Connecting options also require exactly one valid connection
duration per segment gap; supplied connection airport IDs must agree with the
adjacent flights. The sum of flight and connection durations must match
`total_duration`. Incomplete or inconsistent results are skipped rather than
filled with invented data. Missing lookup tokens remain `None`. If every result
is malformed, the returned list is empty. No raw API dictionaries are retained.

Use `provider.get_return_options(selected_option, original_query)` to retrieve
completed `RoundTripItinerary` objects. Pass the unchanged `TripQuery` used for
`search()`: the provider resends its original departure/arrival airports and both
dates, with the same round-trip, economy, USD, and locale settings, plus the
selected option's token. It checks airport and currency agreement before making
the request. Dates are not stored in `FlightOption`, so callers are responsible
for retaining and supplying the original query. A missing or blank token raises
`FlightProviderError` without making a request.

Return choices use the same segment/connection normalizer as outbound choices.
Each completed itinerary preserves the selected outbound and adds one inbound.
Its `total_price` comes from that return result's `price`, which SerpApi's
[returning-flight example](https://serpapi.com/google-flights-results) labels as
a round-trip fare. It is neither added to the initial quote nor replaced by it.
Missing or invalid prices, contradictory trip types, malformed itineraries, and
return routes that do not reverse the outbound endpoints are skipped individually.
Both result groups are combined; no usable returns produces `[]`. Completed
itineraries contain no departure or booking tokens and retain no raw API data.
The outbound CLI remains a single-search tool; it does not perform return lookup.

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
`api_key` and `departure_token` parameter values in messages and exception text
before handlers receive the record. Request-scoped context also redacts bare and
URL-encoded copies of the active key and token, including echoed exception text.
That context is cleared on success or failure and isolates concurrent requests.
Logging levels and
application logging stay unchanged, and repeated searches reuse the filter.
The real outgoing query parameter is unchanged. Tests exercise the real
Requests/urllib3 logging path with a mocked connection and blocked sockets/DNS.

Redaction is an output safeguard, not memory isolation: the environment,
request URL, and debugger-visible locals still contain credentials. Lookup tokens
also remain in the selected `FlightOption` and its internal serialization. Developer tools
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

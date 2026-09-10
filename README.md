# FareSentry

A personalized flight-shopping agent for the Agents for Humans hackathon.
MVP-001 retrieves real outbound flight choices from SerpApi Google Flights and
normalizes them into domain models. A selected outbound choice can now retrieve
compatible return options as completed round-trip itineraries. A Strands
recommendation adapter now chooses among acceptable round trips using explicit
soft preferences and deterministic facts. A local SQLite repository stores fare
observations and computes deterministic history statistics. A pure alert evaluator
uses prior-run history and explicit policy to decide which opportunities to
surface. `run_monitoring_cycle()` connects these components for one complete
watch check with bounded return lookups. Frontend, notifications, and AWS
infrastructure are not implemented.

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

Runtime dependencies are `strands-agents`, `boto3[crt]`, `pydantic`, `requests`,
and `python-dotenv`. The `dev` extra installs `pytest` and `ruff`.
The declared `boto3[crt]>=1.41,<2` dependency enables AWS Common Runtime (CRT)
support for consuming `aws login` credentials. Strands' plain Boto3 dependency
does not enable that extra. A normal project install includes the compatible
CRT package; no separate manual SDK installation is needed.

## Bedrock local setup (Windows PowerShell)

Complete the local setup above first, creating `.venv` only if needed and
installing the project with `.\.venv\Scripts\python.exe -m pip install -e '.[dev]'`.
Activation remains optional because every Python command uses `.venv` explicitly.
Install or update the [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)
separately; `aws login` requires CLI version 2.32.0 or later. The AWS identity
must have permission to sign in for local development and invoke the chosen
Bedrock model. See [Boto3's login prerequisites](https://docs.aws.amazon.com/boto3/latest/guide/credentials.html#login-with-console-credentials).

Run these commands in the same PowerShell session used for the recommendation:

```powershell
aws --version
aws login --profile faresentry
$env:AWS_PROFILE = "faresentry"
$env:AWS_REGION = "us-west-2"
$env:FARESENTRY_BEDROCK_MODEL_ID = "global.anthropic.claude-sonnet-4-6"
```

Complete the browser sign-in opened by `aws login`. Boto3 and Strands use the
selected profile's CLI-managed login session. Repeat `aws login --profile faresentry`
when the session expires, and set the environment variables again in a new
PowerShell session. This profile, region, and model configuration was used for
the successful local Bedrock recommendation smoke test.

Before the first Anthropic invocation, complete the one-time Bedrock use-case
form for the AWS account. Select an Anthropic model in the Bedrock console model
catalog and submit use-case details if required. A fresh clone does not require
resubmission if the account has already completed it. See [Anthropic model access](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html).

Do not put AWS credentials in `.env`, source code, or committed files. Use the
CLI-managed login session; the environment variables above select configuration
and contain no credentials. The `.env` setup below is for the SerpApi key only.

If an older environment raises `MissingDependencyException` while loading
`aws login` credentials, reinstall the declared dependencies into the same venv:

```powershell
& .\.venv\Scripts\python.exe -m pip install -e '.[dev]'
& .\.venv\Scripts\python.exe -m pip check
& .\.venv\Scripts\python.exe -c "import boto3, awscrt; print('Boto3 and AWS CRT imports succeeded')"
```

The import check makes no AWS calls and prints no credentials. Missing CRT
support caused this error during initial setup; the declared extra prevents
relying on an unrecorded one-off `pip install "boto3[crt]"` fix. Continue with the
[recommendation API example](#recommendations) after authentication and model access.

## Checks

```powershell
& .\.venv\Scripts\python.exe -m pytest
& .\.venv\Scripts\python.exe -m ruff check .
& .\.venv\Scripts\python.exe -m ruff format --check .
```

Tests use synthetic responses and mocks, require no credentials, and block
unmocked Requests calls, AWS API calls, and Botocore HTTP (including credential
metadata). They never make real SerpApi or Bedrock requests. A scripted local
model also exercises the real Strands structured-output event loop.

## Structure

```text
pyproject.toml                 Dependencies, packaging, pytest and Ruff settings
src/faresentry/
    __init__.py
    models.py                 Trip queries, outbound choices, and typed itineraries
    constraints.py            Deterministic hard-constraint evaluation
    recommendations.py        Typed candidate summaries, comparisons, output validation
    history.py                Watch identity, observations, and Decimal statistics
    alerts.py                 Pure alert policy, signals, and opportunity decisions
    persistence/
        sqlite.py             Versioned SQLite fare-history repository
    agents/
        recommendation.py     Injectable Strands recommendation adapter
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
    test_recommendations.py   Facts, preference validation, and offline agent tests
    test_history.py           Temporary SQLite databases and deterministic history tests
    test_alerts.py            Alert policy, run-aware references, and decision semantics
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

## Recommendations

`TravelerSoftPreferences` supports a preferred stop maximum per direction,
preferences for shorter total travel and connections, dislike of airport changes,
preferred airlines, and `none` / `low` / `moderate` / `high` willingness to pay
more for convenience. These fields never exclude an itinerary. Preferred airline
matching uses normalized airline labels, ignoring case; it does not resolve aliases.

`build_recommendation_request()` is pure Python. It accepts a mapping of stable,
unique candidate IDs to already-filtered `RoundTripItinerary` objects, preferences,
and the active `HardTravelConstraints`. It defensively rechecks every candidate
with the existing evaluator and rejects the entire request if any candidate fails.
It also rejects empty sets, inconsistent currencies, and differing endpoints.
Candidates must come from the same dated search; itinerary models do not carry
dates, so the caller is responsible for that precondition.

Each typed summary contains the full round-trip price, currency, outbound/inbound
duration and stops, ordered connections with transfer endpoints and durations,
airlines and flight numbers, plus convenience totals and preferred-airline matches.
Python supplies signed comparisons for every ordered pair: price premiums,
percentages, travel time, stops, connection time, and transfer counts. Percentages
use the reference price and round half up to two decimal places; a zero reference
price produces `null`. Decimal prices serialize as strings without float conversion.
The comparisons grow quadratically, so this interface is intended for a small shortlist.

`StrandsRecommender` creates a fresh agent on each invocation, using no application
tools and no streaming console callback. Its system prompt limits judgment to
subjective tradeoffs and tells the model to cite supplied facts without arithmetic,
hard-constraint evaluation, or future-price predictions. Hard constraints are not
included in the prompt. Model prose is still subjective output; schema validation
does not prove that every explanation is factually correct.

Example integration after retrieval and deterministic filtering:

```python
import os

from strands.models import BedrockModel

from faresentry.agents.recommendation import StrandsRecommender
from faresentry.models import TravelerSoftPreferences

# accepted_candidates: dict[str, RoundTripItinerary] from one search
# constraints: the same HardTravelConstraints used when filtering
preferences = TravelerSoftPreferences(
    preferred_max_stops_per_direction=1,
    prefer_shorter_total_travel_time=True,
    prefer_shorter_connections=True,
    dislike_airport_transfers=True,
    preferred_airlines=("Singapore Airlines",),
    willingness_to_pay_more="moderate",
)
model = BedrockModel(
    model_id=os.environ["FARESENTRY_BEDROCK_MODEL_ID"],
    region_name=os.environ["AWS_REGION"],
    temperature=0.2,
    max_tokens=1500,
)
recommendation = StrandsRecommender(model=model).recommend(
    accepted_candidates, preferences, constraints=constraints
)
print(recommendation.model_dump_json(indent=2))
```

The caller selects a Bedrock model supporting structured output through tool use
and configures AWS credentials through environment variables or the standard AWS
credential chain. This example performs a live, billable model invocation; tests
inject an agent factory or a scripted SDK model instead. The adapter also accepts
an explicit model ID or another Strands `Model`. There is no hard-coded model ID,
credential, region, or import-time AWS client initialization. The minimum Strands
version is 1.55, matching the SDK version validated locally. See the official
[Strands structured-output documentation](https://strandsagents.com/docs/user-guide/concepts/agents/structured-output/).

The result is a `Recommendation` with `selected_candidate_id`, display-only
`recommendation`, one to six typed `key_tradeoffs`, and `low` / `medium` / `high`
`confidence` describing strength of fit, not a calibrated probability. Each
`RecommendationTradeoff` contains:

- `category`: `price`, `total_travel_time`, `stops`, `connections`,
  `airport_transfer`, `airline_preference`, or `overall_value`.
- `candidate_ids`: one or more distinct supplied candidates discussed in that judgment.
- `favored_candidate_id`: a discussed candidate, or `null` when none is favored
  on that dimension (for example a tie or no preference).
- `explanation`: concise display-only text.

An individual dimension may favor an alternative: a price judgment can favor
`budget` while the final selection is `direct`. `overall_value` is the final
synthesis across preferences; if included, it must favor `selected_candidate_id`.
The selected ID is the sole authoritative final recommendation target.

Every structured candidate reference must belong to the current request.
`parse_recommendation()` performs this membership validation after Pydantic checks
categories and reference relationships, even if the SDK returned nested Pydantic
objects. The Strands adapter always uses this boundary before returning a result.
String-only tradeoffs are no longer supported.

Downstream decision logic **must use structured fields**, never extract candidate
IDs, judgments, or facts from `recommendation` or `explanation` text. These strings
are for display and are not validated for factual correctness; even an invented
name in prose has no machine-readable meaning. Obtain objective numbers from the
deterministic request summaries/comparisons, not model explanations. For example:

```python
selected_trip = accepted_candidates[recommendation.selected_candidate_id]
price_judgments = [
    tradeoff
    for tradeoff in recommendation.key_tradeoffs
    if tradeoff.category == "price"
]
# Each judgment exposes candidate_ids and favored_candidate_id directly.
```

Invalid input raises `ValueError`; malformed output or an unknown structured
ID raises `InvalidRecommendationError`; other SDK/provider failures raise
`RecommendationError`. Both recommendation errors have safe display messages.
There is no fallback selection on failure or alert decision. History is available
through the separate repository below; the recommendation adapter does not write it.

## SQLite fare history

`SQLiteFareHistory` uses Python's standard-library `sqlite3`; no new dependency or
credentials are needed. Supply a dedicated database file with an existing parent
directory. Construction creates the file/schema when absent. Each operation closes
its connection, so repositories can be reopened without a lifecycle/close method.
`:memory:` is intentionally unsupported; tests use pytest `tmp_path` files.

Given the original `query: TripQuery` and a completed `selected_trip:
RoundTripItinerary` from the examples above:

```python
from datetime import UTC, datetime
from pathlib import Path

from faresentry.history import FareWatch
from faresentry.persistence import SQLiteFareHistory

history = SQLiteFareHistory(Path("fares.sqlite3"))
watch = FareWatch.from_query(query)
run = history.create_run(watch, observed_at=datetime.now(UTC))
observation = history.record_observation(watch, selected_trip, run=run)
recent = history.get_recent_observations(watch, limit=20)
lowest = history.get_lowest_price(watch)
statistics = history.get_price_statistics(watch)
same_itinerary = history.get_price_statistics(
    watch, itinerary_id=observation.itinerary_id
)
prior_watch = history.get_prior_observations(watch, before_run=run)
prior_itinerary = history.get_prior_observations(
    watch, before_run=run, itinerary_id=observation.itinerary_id
)
prior_statistics = history.get_prior_run_statistics(watch, before_run=run)
```

The immutable watch uses a versioned SHA-256 hash of canonical origin,
destination, outbound date, return date, and currency. Identical queries share
history; preferences and hard-constraint policies are not part of this raw fare
history. The caller decides which complete fares to record. If cabin, passenger
counts, or additional search filters become configurable, extend the watch and
version its identity before using them. Currency must match the watch, as must
the itinerary's route. Dates come from the original query because normalized
itineraries currently lack dates.

The `watches` table stores the watch ID, validated context JSON, and currency.
`fare_observations` stores an insertion ID, watch ID, UTC observation timestamp,
Decimal price text, currency, itinerary ID, typed outbound/inbound JSON, and an
optional run ID. `monitoring_runs` stores a database-local integer run ID, watch
ID, explicit UTC timestamp, and a checked `completed` flag.
These normalized directions retain flight numbers, airlines, airports, flight
durations, and connections; stops and total durations are derived from them.
No provider tokens, raw SerpApi dictionaries, credentials, or LLM responses are
accepted or stored. The itinerary hash includes the watch and both normalized
directions, excluding price. It remains stable across price changes, but changes
when durations/connections change. Without schedule timestamps in the current
domain model, it cannot uniquely identify every scheduled flight.

Create one run per search/check and pass the same run to every observation from
that check. `create_run()` requires a timezone-aware timestamp and normalizes it
to UTC. Runs order by `(observed_at, run_id)`; timestamp ties use run ID and
backdated runs sort by their explicit timestamp, regardless of when their fares
are inserted. Run objects must match persisted metadata in the repository and
belong to the observation's watch. Run-bound observations use the run timestamp;
an explicitly supplied observation timestamp must represent that same instant.
New runs begin incomplete. After preparing a normal outcome, call
`history.mark_run_completed(watch, run=run)` to make the run's observations
eligible for future alert history. Repeating completion is harmless; unknown runs
or mismatched run metadata raise `ValueError`. The one-cycle runner handles this
automatically. Completion is persisted state, separate from immutable run identity.

There is one canonical observation per `(run_id, itinerary_id)`. Repeating the
same normalized itinerary and numerically equal Decimal fare returns the original
record, preserving its ID and Decimal representation. A conflicting fare or
details under that identity raises `ValueError`; a mismatched currency is rejected.
The identity helper derives itinerary IDs from normalized directions, so changed
flight/duration/connection details normally identify a different itinerary.
Duplicate lookup and insertion are transactional, with a unique index enforcing
the rule across concurrent writers.

For backwards compatibility, `record_observation()` without a run still appends
ungrouped observations, defaulting the timestamp to now in UTC. These have null
run membership and cannot be used as run-aware alert history. No run boundaries
are inferred from timestamps, insertion IDs, itinerary IDs, or prices.

`get_prior_observations(watch, before_run=run)` includes only completed runs strictly
preceding the supplied run. It excludes every observation in the current run,
later runs, incomplete/failed runs, and all null-run records. Results order by run
timestamp, run ID, then observation ID within each run. Use `itinerary_id` for same-itinerary history;
omit it for whole-watch history across different itineraries. The itinerary does
not need to recur in another run. Within-run records are alternatives, not
temporal fare changes. Each query reads one database snapshot. Application callers
should finish writing a run's observations before marking it completed.

`get_prior_run_statistics()` accepts the same scope and returns
`PriorRunPriceStatistics`: the boundary run, currency, itinerary filter, observation
count, represented run count, minimum, maximum, and average. Empty runs do not
contribute to run count. Each canonical observation counts once, so watch-level
averages are observation-weighted, not run-weighted. No arbitrary candidate is
designated as the previous run's fare. Empty scopes return zero counts and null
prices. This API does not compare the current fare or make alert decisions.

The original `get_recent_observations()`, `get_lowest_price()`, and
`get_price_statistics()` retain their record-based semantics, including ungrouped
history and observations from incomplete runs. Recent reads sort by observation
timestamp then insertion ID; `limit` selects the latest N observations and returns
them oldest first. These APIs do
not establish temporal run boundaries.

Statistics expose count, minimum, maximum, average, current price, previous price,
historical low, and signed current-minus-reference differences. Current is the
latest observation by timestamp/insertion ID, previous is its predecessor, and
historical low is the minimum **before current**. Minimum/maximum/average include
current. A new record low therefore produces a negative
`current_vs_historical_low`. Missing comparisons are `None`; empty history has
count zero, no prices, and `get_lowest_price()` returns `None`.

Prices use SQLite `TEXT` and round-trip through `Decimal` without conversion to
float or forced currency rounding. Numeric comparisons and aggregation run in
Python, never SQLite `AVG`/`SUM` or lexical `MIN` on text. Calculations use a local
Decimal context with at least 28 significant digits, increased as necessary to
preserve exact sums/differences. Repeating averages round half-even at that
precision. Results do not depend on the caller's Decimal precision or rounding.
Queries currently load their scoped history into memory, appropriate for the
initial local store.

Schema version 3 is tracked with `PRAGMA user_version`. Version 1 is migrated
transactionally by adding the run table, nullable observation membership, indexes,
and watch/run consistency triggers, followed by the version-3 completion marker.
Version 2 migrates by adding `completed INTEGER NOT NULL DEFAULT 0` constrained to
0 or 1. All preexisting v2 runs are marked completed to preserve existing history;
new runs default to incomplete. Observations are unchanged, and v1 observations
retain null run membership; no legacy runs are invented. Initialization/migration
is repeatable, and a failure rolls back both schema changes and the version.
Foreign keys enforce watch/currency and run existence. An empty version-0 database
is initialized, while nonempty unversioned databases and unsupported versions are
rejected without modification. The existing `.gitignore` excludes
`*.db` and `*.sqlite3`. Persistence does not automatically record fares or make
alert decisions; the explicit decision API below consumes its normalized data.

## Deterministic opportunity decisions

`faresentry.alerts.evaluate_alert()` consumes a structured `Recommendation`, the
selected complete itinerary and current `FareObservation`, a `MonitoringRun`,
whole-watch history, active hard constraints, and an explicit `AlertPolicy`.
It performs no I/O and calls no provider, agent, or notification service.

Using the recommendation and history objects from the preceding examples:

```python
from decimal import Decimal

from faresentry.alerts import AlertPolicy, evaluate_alert

decision = evaluate_alert(
    watch=watch,
    current_run=run,
    candidate_id=recommendation.selected_candidate_id,
    itinerary=selected_trip,
    current_observation=observation,
    history=history.get_prior_observations(watch, before_run=run),
    recommendation=recommendation,
    constraints=constraints,
    policy=AlertPolicy(
        currency=watch.currency,
        minimum_absolute_improvement=Decimal("50"),
        minimum_percentage_improvement=Decimal("5"),
        target_price=Decimal("1000"),
        historical_low_mode="sufficient",
        first_observation="suppress",
    ),
)
print(decision.model_dump_json(indent=2))
# After all normal outcome preparation succeeds:
history.mark_run_completed(watch, run=run)
```

The candidate ID must equal `Recommendation.selected_candidate_id`. The itinerary
must match the current observation's normalized directions, fare, and currency;
that observation must belong to the supplied watch and current run. Current hard
constraints are checked defensively using the existing deterministic evaluator.
Recommendation prose and confidence do not affect alert rules. Invalid identity,
currency, constraint, or run inputs raise errors rather than produce an alert.

Supply whole-watch history from the same repository. Do not use an itinerary
filter or a recent-record limit: those omit facts needed for watch-level
references and first-observation detection. The one-cycle application API below
filters this history against active hard constraints to define eligible fare
opportunities; the pure evaluator does not filter historical constraints itself.
The evaluator additionally excludes null-run membership and all runs at or
after the current run. Completion is database state unavailable in a
`FareObservation`, so callers must use the completion-filtered prior-history API;
do not substitute raw `get_recent_observations()` results for alert references.
Run-bound observation timestamps equal their run timestamp
under the repository contract, so comparisons use `(observed_at, run_id)` and
never observation insertion order. Contradictory run timestamps and duplicate
prior `(run_id, itinerary_id)` records are rejected. As a pure function, it cannot
authenticate persisted provenance or detect history that the caller omitted.

The reference semantics are explicit:

- `previous_same_itinerary_price` is the last price of that itinerary in eligible
  prior runs. Another itinerary's price is never labeled its previous price.
- `prior_watch_low` and `prior_watch_average` cover all eligible observations in
  the watch. The average is observation-weighted and uses the existing Decimal
  history rounding. These are observed fares, not a record of previously selected
  recommendations or evidence of historical constraint approval.
- Improvement uses the previous same-itinerary price when available. A new
  itinerary instead uses the prior watch low. The decision exposes both
  `improvement_reference_type` and `improvement_reference_price`.
- Absolute improvement is reference minus current price, with no money rounding.
  Percentage improvement is `(reference - current) * 100 / reference`, rounded to
  two decimal places with `ROUND_HALF_UP`. Threshold comparison uses that rounded
  percentage. A zero reference makes percentage improvement unavailable. Numeric
  calculations use a fresh Decimal context, independent of caller settings.

`should_alert` requires **every prerequisite and at least one trigger**:

- When both improvement thresholds are configured, **both must pass** for the
  improvement trigger. A single configured threshold must pass on its own; no
  configured thresholds means no improvement trigger. Thresholds are positive,
  with percentage thresholds at most 100. Equality meets a threshold.
- A configured target is both an inclusive ceiling and an independent trigger:
  `current_price <= target_price`. Missing the target blocks every trigger.
- A strict new low means `current_price < prior_watch_low`; equality is not a new
  low. Mode `ignore` makes this informational, `sufficient` makes it an independent
  trigger, and `required` makes it a prerequisite that still needs another trigger.
- With no eligible prior watch history, `first_observation="suppress"` blocks
  alerting; `"alert"` permits it and supplies a trigger. Target and required-low
  prerequisites still apply. A required historical low cannot be established with
  empty history. Previous price, low, average, and improvements remain null.

For example, a newly recommended $975 itinerary can meet a $1,000 target even if
the prior watch low was $950. It needs no same-itinerary history. Without another
trigger, unchanged fares and price increases do not alert. Target hits may alert
again on a later run; this foundation has no delivery state or notification log.
Repeated evaluation of identical data deliberately returns the identical decision.

`AlertDecision` contains immutable source facts and policy. Arithmetic, seven
typed `AlertSignal` records, and `should_alert` are computed fields, not independent
caller inputs. Each signal includes its role (prerequisite, trigger, both,
component, or information), satisfaction, actual/reference values, and a
deterministic explanation. Optional disabled rules remain visible but cannot
trigger. Ordinary JSON serialization includes all computed fields and serializes
Decimal values as strings. Use `model_dump_json(round_trip=True)` to save source
state that can be reloaded with `AlertDecision.model_validate_json()`; derived
fields are then recomputed. Alert numeric inputs reject floats, booleans, and
nonfinite values. No scheduling or notification delivery is implemented.

## One monitoring cycle

`faresentry.monitoring.run_monitoring_cycle()` is the synchronous application
entry point. Inject a `FlightProvider`, a `FareHistoryRepository` (implemented by
`SQLiteFareHistory`), and a `Recommender` (implemented by `StrandsRecommender`).
The orchestration module imports neither the SerpApi implementation nor the
Strands SDK. Using the configured `provider`, `query`, `constraints`, `preferences`,
`history`, and `model` from the examples above:

```python
from faresentry.monitoring import run_monitoring_cycle

result = run_monitoring_cycle(
    query,
    constraints=constraints,
    preferences=preferences,
    policy=AlertPolicy(
        currency=query.currency,
        minimum_absolute_improvement="100",
        first_observation="suppress",
    ),
    provider=provider,
    history=history,
    recommender=StrandsRecommender(model=model),
    max_return_lookups=3,
)
print(result.model_dump_json(indent=2))
```

Each call creates exactly one persisted monitoring run and performs one initial
search. Choices with missing/blank departure tokens or failing outbound hard
constraints are skipped. The first `max_return_lookups` remaining choices, in
provider order, receive return lookups. The default is 3; the limit must be a
positive integer. Quoted outbound-choice prices do not reorder provider results.
All completed returns from those lookups are considered, so the limit bounds
return-lookup calls, not the number of candidates or provider-internal retries.

Completed itineraries are checked against both-direction hard constraints.
Identical normalized itinerary identities and equal fares are deduplicated in
first-seen order. Conflicting fares under one identity raise
`ConflictingItineraryError` before any observations are written. The existing
identity excludes price and lacks schedule times, cabin, baggage, and other
fare-product details; the runner cannot distinguish those products or silently
choose a representative fare. Stable `itinerary_identity()` hashes serve as
candidate IDs in both recommendation and persistence.

Only eligible unique completed fares are recorded, all under the current run,
before the single recommendation call. Every eligible candidate is supplied to
that call, and all returned structured references are checked against that set.
The selected ID maps to `result.selected_itinerary`. No recommendation prose is
parsed and no new agent behavior is added.

After recommendation, `get_prior_observations(watch, before_run=run)` retrieves
the completed prior watch scope in one snapshot. It excludes the current run,
later runs, incomplete/failed runs, and legacy null-run observations. Since watch
identity excludes hard constraints, the runner rechecks prior fares against the active constraints,
also protecting against ineligible records written by other callers. The existing
alert evaluator derives same-itinerary previous price, watch low, and watch
average from that eligible prior scope. Relaxing constraints cannot recover
fares that previous cycles never recorded; history represents observed eligible
opportunities, not an exhaustive market history. Raw repository statistics retain
their existing semantics.

`MonitoringRunResult` includes watch/run IDs, status, outbound and lookup counts,
unique completed/rejected/eligible counts, duplicate count, and optional selected
itinerary, `Recommendation`, and `AlertDecision`. Outbound prescreen rejections
are counted separately. Status `completed` means an alert decision was evaluated;
inspect `result.alert_decision.should_alert` for its answer. Normal empty outcomes
are `no_outbound_options`, `no_usable_departure_tokens`,
`no_eligible_outbound_options`, `no_completed_itineraries`, and
`no_eligible_candidates`. They retain the run and counts but have no recommendation
or alert decision.

Provider, persistence, and recommendation failures propagate; no retries or
fallback selections turn failures into empty results. The cycle is not one
database transaction across external calls: an already-created run and any
committed eligible observations remain if a later step fails. A new invocation
creates a new run. A run is marked completed only after its structured result has
been prepared, including every normal empty outcome and `should_alert=False`.
Saving completion is the last persistence operation before returning; failure to
save the marker propagates. Provider, observation-write, recommendation/validation,
history-read, alert-evaluation, or result-preparation failures leave the run
incomplete. Retained observations from these runs remain available through raw
history APIs but are excluded from future alert baselines, including previous
same-itinerary price, watch low, and watch average. No long-lived transaction spans
provider or model work. No raw responses, tokens, or credentials enter history.
For deterministic tests, inject `clock=lambda: aware_datetime`; it is called once
per valid check. Tests use scripted dependencies and temporary SQLite files,
with real HTTP and AWS calls blocked. This API performs no scheduling or delivery.

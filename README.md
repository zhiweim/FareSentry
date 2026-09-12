# FareSentry

**Autonomous airfare monitoring that only interrupts you when it matters.**

Shopping for an international trip means weighing price against long journeys,
extra stops, and inconvenient connections. A lower fare is not always a better
trip. FareSentry helps travelers who want a worthwhile option that fits their
preferences, without repeatedly checking flight searches themselves.

Built for the **Agents for Humans hackathon**, FareSentry is a personalized
airfare-monitoring agent for **pre-booking flight shopping**. Travelers configure
a dated round trip, hard constraints, soft preferences, an alert policy, and a
monitoring interval. The local monitoring loop checks current options, compares
acceptable itineraries, and uses fare history to decide when an alert is warranted.
A completed check that stays silent is a successful outcome.

**[Try the public demo → faresentry.streamlit.app](https://faresentry.streamlit.app/)**

The hosted demo uses **deterministic synthetic fares, scripted recommendations,
and simulated email** for repeatable judging. Real SerpApi, Strands + Amazon
Bedrock, and Amazon SES integrations were validated separately against live
services locally. The public app runs neither live-service calls nor unattended
background monitoring.

**Explore:** [Public Demo](#public-demo) · [Architecture](#architecture) ·
[Quick Start](#quick-start--local-demo) · [Live Mode](#live-mode) ·
[Repository Structure](#repository-structure) · [Limitations](#limitations--whats-next) ·
[Technical Documentation](docs/TECHNICAL.md)

## Why FareSentry?

A simple price-change alert answers “did the price change?” FareSentry also asks
whether the current itinerary is acceptable and worth considering for this
traveler. Stops, total duration, airport transfers, airlines, and stated
preferences shape the choice. A cheaper itinerary can fail a hard limit, while
a modest premium can be worthwhile for a simpler journey.

## How It Works

```text
User watch configuration
  → local scheduler / monitoring cycle
  → SerpApi Google Flights outbound search + bounded return lookups
  → deterministic normalization + hard-constraint filtering
  → SQLite eligible fare observations
  → Strands + Bedrock subjective recommendation
  → deterministic alert evaluation using prior completed, eligible history
  → no alert: silence
    OR approved alert: notification service → Amazon SES
```

**AI chooses among acceptable options. Python decides whether the user should
be interrupted.**

The scheduler checks which watches are due and can repeat in a caller-run local
polling loop. Persisted attempts preserve cadence across restarts; completed
observations provide historical context. The Streamlit page runs explicit checks,
not that unattended loop. Historical observations are comparison evidence, not
inventory assumed to remain bookable.

## Architecture

- **Deterministic Python:** validates models, calculates objective facts, enforces
  hard constraints, manages run/history state, evaluates alert policy, and gates
  notification delivery.
- **Strands Agents + Amazon Bedrock:** judge subjective price-versus-convenience
  tradeoffs among supplied eligible candidates and return a validated structured
  recommendation. The agent does not search through flight tools or decide alerts.
- **Provider adapters:** isolate SerpApi retrieval and SES transport from domain
  logic. SQLite stores observations and successful delivery receipts; Streamlit
  presents results. Tests substitute external services without replacing the
  deterministic application logic.

## Public Demo

Open **[faresentry.streamlit.app](https://faresentry.streamlit.app/)**, choose a
story, and click **Run FareSentry Check**. No login or credentials are needed.

- **Routine check:** the selected fare remains USD 1,000 → **completed + silent**.
  No simulated notification is delivered.
- **Worthwhile opportunity:** the selected fare drops from USD 1,000 to USD 800
  → **alert + simulated notification**.

Synthetic inputs keep both scenarios reproducible. Each run creates isolated
temporary SQLite history and exercises the existing monitoring, scheduler, alert
evaluator, and notification flow. The recommendation is scripted; `should_alert`
is calculated by the real policy evaluator.

The public app runs on **Streamlit Community Cloud with Python 3.13**. Live Mode
is intentionally disabled publicly. For host configuration and update checks,
see [deployment notes](docs/TECHNICAL.md#public-hackathon-deployment-streamlit-community-cloud).

## Real Integrations

- **SerpApi Google Flights:** real flight retrieval validated locally; the adapter
  implements outbound search and compatible return completion.
- **Strands Agents + Amazon Bedrock:** live structured recommendation smoke test
  completed with an explicitly configured Bedrock model.
- **Amazon SES:** real email transport smoke test completed separately. SES
  acceptance is not a guarantee of inbox delivery or exactly-once sending.
- **SQLite:** real local persistence used by monitoring and by synthetic demos;
  run/history and delivery-receipt behavior is covered by automated tests.

These live integration checks are separate from the credential-free public demo
and the mocked/synthetic automated test suite.

## Technology

- **Runtime and domain:** Python 3.13, Pydantic, Decimal arithmetic, SQLite.
- **AI and external services:** Strands Agents, Amazon Bedrock, SerpApi Google
  Flights, Amazon SES, Boto3 with AWS CRT support, Requests.
- **Interface and hosting:** Streamlit, Streamlit Community Cloud, GitHub.
- **Development:** pytest, Ruff, setuptools, python-dotenv for the SerpApi
  smoke script's local configuration.

## Validation

Final hackathon validation on **Python 3.13**:

- **636 automated tests passing**.
- **Ruff lint and formatting checks passing**.
- **Public Streamlit demo validated**, including both scenarios, reload
  repeatability, desktop/mobile usability, and visible-output safety.
- **Real SerpApi, Bedrock, and SES smoke tests completed locally**.

Automated tests block real HTTP/AWS calls. The commands and test boundaries are
documented under [Checks](docs/TECHNICAL.md#checks).

## Quick Start / Local Demo

Use Windows PowerShell from the repository root with Python **3.13** installed.
Create the local virtual environment if it does not exist:

```powershell
if (-not (Test-Path .\.venv)) {
    py -3.13 -m venv .venv
}
& .\.venv\Scripts\python.exe --version
```

Confirm the environment reports Python 3.13 before installing dependencies.
If an existing environment uses a different version, follow the
[local setup notes](docs/TECHNICAL.md#local-setup-windows-powershell).

Install the app and development tools, then launch Streamlit:

```powershell
& .\.venv\Scripts\python.exe -m pip install -e '.[dev,ui]'
& .\.venv\Scripts\python.exe -m streamlit run .\streamlit_app.py --server.address=127.0.0.1
```

Open **http://127.0.0.1:8501**. Demo Mode is ready without AWS login or service
configuration; use the stories described under [Public Demo](#public-demo).
Stop the server with **Ctrl+C**.

These commands invoke the virtual environment explicitly; activation is
unnecessary. There is no frontend build or separate API server to start.
For more about temporary demo state and rerendering, see
[local demo behavior](docs/TECHNICAL.md#local-hackathon-demo).

### Run the checks

After installing `.[dev,ui]`:

```powershell
& .\.venv\Scripts\python.exe -m pytest --basetemp="$env:USERPROFILE\faresentry-pytest-temp" -p no:cacheprovider
& .\.venv\Scripts\python.exe -m ruff check .
& .\.venv\Scripts\python.exe -m ruff format --check .
```

The pytest temporary path is dedicated test scratch space; pytest recreates it.
Tests use synthetic inputs and injected services, not live credentials.
See [testing details](docs/TECHNICAL.md#checks).

## Live Mode

Live Mode is **optional and local-only**. Enable it in the PowerShell session
that launches Streamlit:

```powershell
$env:FARESENTRY_ENABLE_LIVE = "1"
```

Configure these environment variables before starting the app:

- `SERPAPI_API_KEY`
- `AWS_PROFILE`
- `AWS_REGION`
- `FARESENTRY_BEDROCK_MODEL_ID`

Use standard AWS authentication; never put AWS credentials in source code or
`.env`. The UI reads exported environment variables and does not load `.env`
automatically. **Live requests can incur service charges.**

Submit **Run live FareSentry check** for one current search and recommendation.
The first observation establishes history and stays silent. Subsequent checks
use the configured improvement threshold.

Real email also requires `FARESENTRY_EMAIL_FROM` and `FARESENTRY_EMAIL_TO`.
For an approved result, use the separate **Send this approved alert by real
email** button; a live check never sends automatically. Use one active Live Mode
session per local database.

Full instructions:

- [Local Live Mode behavior and storage](docs/TECHNICAL.md#live-mode)
- [AWS login and Bedrock setup](docs/TECHNICAL.md#bedrock-local-setup-windows-powershell)
- [SerpApi setup and search smoke test](docs/TECHNICAL.md#serpapi-local-setup-and-real-data-smoke-test)
- [SES configuration](docs/TECHNICAL.md#ses-configuration)
- [Explicit email smoke test](docs/TECHNICAL.md#explicit-manual-transport-smoke-test)

## Repository Structure

The main components are small Python modules with external services behind
provider boundaries. See the [complete structure](docs/TECHNICAL.md#structure)
for the individual test modules.

```text
streamlit_app.py                 Streamlit page and explicit UI actions
pyproject.toml                   Dependencies and tool settings
requirements.txt                 Hosted install of the ui extra
src/faresentry/
    models.py                   Queries, itineraries, preferences, recommendations
    constraints.py              Deterministic hard-constraint checks
    recommendations.py          Candidate facts, comparisons, output validation
    alerts.py                   Deterministic alert policy and decisions
    monitoring.py               One bounded monitoring cycle
    scheduling.py               Sequential due-watch checks and local polling
    notifications.py            Formatting, delivery gating, deduplication
    notification_models.py      Typed messages and delivery outcomes
    history.py                  Watch/run identity and fare observations
    persistence/sqlite.py       SQLite history and successful-send receipts
    providers/base.py           Flight provider contract
    providers/serpapi.py         Google Flights retrieval and normalization
    providers/ses.py             Amazon SES transport
    agents/recommendation.py     Strands structured recommendation adapter
    demo.py                     Synthetic scenarios using application services
    demo_live.py                Optional local live-service composition
scripts/
    search_flights.py           Manual real-data smoke test
    send_test_email.py          Explicit SES transport smoke test
tests/                          Domain, provider, orchestration, and UI tests
docs/TECHNICAL.md                Detailed implementation and setup reference
LICENSE                         MIT License
```

## Limitations / What's Next

- No production accounts, authentication, multi-user isolation, or durable full
  watch/preferences/schedule configuration.
- Fixed-date round trips only; no flexible dates, multi-city/open-jaw, automatic
  booking, or post-booking cancellation/rebooking optimization.
- Historical baselines are MVP comparisons of observed fares, not a fair-price
  model or future-fare prediction engine. Old observations are not assumed bookable.
- Itinerary identity omits full schedule timestamps and fare-product detail.
  Bounded exploration in provider order can miss other options.
- Local sequential scheduling and SQLite are not distributed production
  infrastructure. Email has no automatic retry/outbox recovery or exactly-once
  guarantee after an uncertain send.

Possible next steps include recency-aware market baselines, richer cold-start
handling beyond the existing first-observation policy, flexible dates and
open-jaw/multi-city search, post-booking change economics, and a durable cloud
worker with persistence. These are future directions, not current features.

## Technical Documentation

For detailed implementation notes covering provider normalization, structured
recommendations, SQLite persistence, alert semantics, scheduling, notification
delivery, and live-service setup, see **[docs/TECHNICAL.md](docs/TECHNICAL.md)**.

Useful starting points:

- [Models, normalization, and return lookup](docs/TECHNICAL.md#models-and-serpapi-normalization)
- [Structured recommendations and validation](docs/TECHNICAL.md#recommendations)
- [SQLite schema, migrations, and historical queries](docs/TECHNICAL.md#sqlite-fare-history)
- [Alert reference rules and policy signals](docs/TECHNICAL.md#deterministic-opportunity-decisions)
- [Monitoring completion and failure semantics](docs/TECHNICAL.md#one-monitoring-cycle)
- [Scheduler cadence and restart behavior](docs/TECHNICAL.md#local-watch-scheduling)
- [Notification receipts and SES delivery caveats](docs/TECHNICAL.md#email-notification-delivery)

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).

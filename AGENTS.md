# FareSentry

FareSentry is a submission for the Agents for Humans hackathon.

It is a personalized flight-shopping agent built with:
- Python 3.13
- Strands Agents
- Amazon Bedrock
- SerpApi Google Flights
- Pydantic
- SQLite initially
- pytest

## Product goal

FareSentry monitors flight options for a user's international trip.

It should:
1. understand travel preferences,
2. retrieve flight options,
3. reject flights violating hard constraints,
4. compare acceptable itineraries,
5. store fare history,
6. decide whether a change is worth alerting the traveler about,
7. clearly explain its recommendation.

FareSentry is not just a cheapest-price tracker. It evaluates price versus
inconvenience according to the user's preferences.

## Architecture rules

Keep deterministic logic separate from LLM judgment.

Python code should calculate:
- price differences
- percentages
- number of stops
- durations
- layover durations
- hard constraint violations
- basic ranking metrics

The Strands agent should reason about:
- tradeoffs between acceptable itineraries
- whether an observed fare change is noteworthy
- which itinerary best matches the user
- explanations and recommendations

Do not ask the LLM to perform calculations ordinary Python can perform.

## Initial scope

For the first milestone:
- support one origin
- support one destination
- support one outbound date
- support one return date
- retrieve real flight data
- normalize API responses
- compare several options

Do NOT implement yet:
- frontend
- scheduled jobs
- AWS deployment
- notifications
- multi-agent architecture
- authentication
- automatic purchasing
- complex RAG

## Engineering conventions

- Use type hints.
- Use Pydantic models for domain data.
- Keep external API code behind a provider abstraction.
- Never commit credentials.
- Read credentials from environment variables or standard AWS credential mechanisms.
- Use pytest for meaningful unit tests.
- Prefer simple code over premature abstractions.
- Run tests after implementing a feature.

## Windows development environment

Development is on Windows using PowerShell.

The project virtual environment is `.venv`.

When running Python-related commands, prefer invoking the virtual environment
explicitly rather than assuming it is activated:

- `.\.venv\Scripts\python.exe`
- `.\.venv\Scripts\python.exe -m pytest`
- `.\.venv\Scripts\python.exe -m pip`
- `.\.venv\Scripts\python.exe -m ruff`

Do not use Unix commands such as:
- `source .venv/bin/activate`
- `.venv/bin/python`

If `.venv` does not exist, create it using the project-local Python version
configured by pyenv before installing dependencies.
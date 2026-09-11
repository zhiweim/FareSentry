"""Demo-first page over FareSentry services; actions require explicit clicks."""

from datetime import date, timedelta

import streamlit as st
from pydantic import ValidationError

from faresentry.alerts import AlertPolicy
from faresentry.demo import (
    CheckReport,
    demo_watch,
    history_rows,
    run_demo,
    safe_recommendation_text,
)
from faresentry.demo_live import (
    deliver_live_alert,
    live_mode_enabled,
    missing_live_configuration,
    run_live_check,
)
from faresentry.history import FareWatch
from faresentry.models import HardTravelConstraints, TravelerSoftPreferences, TripQuery
from faresentry.scheduling import ScheduledWatch


def watch_form() -> ScheduledWatch | None:
    """Create existing validated domain models only when the form is submitted."""
    with st.form("live_watch"):
        st.subheader("Define your trip")
        a, b = st.columns(2)
        origin = a.text_input("Origin airport", "LAX")
        destination = b.text_input("Destination airport", "SIN")
        outbound = a.date_input("Departure date", date.today() + timedelta(days=60))
        inbound = b.date_input("Return date", date.today() + timedelta(days=74))
        st.caption("Hard limits · itineraries outside these limits are rejected")
        a, b, c = st.columns(3)
        stops = a.number_input("Maximum stops per direction", 0, 3, 1)
        duration = b.number_input("Maximum minutes per direction", 1, 5000, 1400)
        connection = c.number_input("Maximum connection minutes", 0, 2000, 240)
        transfers = st.checkbox("Allow airport transfers", value=False)
        st.caption("Soft preferences · guide the agent's tradeoff judgment")
        a, b = st.columns(2)
        preferred_stops = a.number_input("Preferred maximum stops", 0, 3, 0)
        willingness = b.selectbox(
            "Pay more for convenience", ["none", "low", "moderate", "high"], index=2
        )
        shorter = a.checkbox("Prefer shorter total travel", value=True)
        shorter_connections = b.checkbox("Prefer shorter connections", value=True)
        airline = st.text_input("Preferred airline (optional)")
        a, b = st.columns(2)
        threshold = a.text_input("Minimum fare improvement (USD)", "100")
        cadence = b.number_input("Monitoring interval (hours)", 1, 168, 6)
        st.caption(
            "USD round trips. The first observation stays silent to establish history."
        )
        submitted = st.form_submit_button("Run live FareSentry check", type="primary")
    if not submitted:
        return None
    try:
        return ScheduledWatch(
            watch=FareWatch.from_query(
                TripQuery(
                    origin=origin.strip().upper(),
                    destination=destination.strip().upper(),
                    outbound_date=outbound,
                    return_date=inbound,
                )
            ),
            constraints=HardTravelConstraints(
                max_stops_per_direction=stops,
                max_duration_minutes_per_direction=duration,
                max_connection_duration_minutes=connection,
                allow_airport_transfers=transfers,
            ),
            preferences=TravelerSoftPreferences(
                preferred_max_stops_per_direction=preferred_stops,
                prefer_shorter_total_travel_time=shorter,
                prefer_shorter_connections=shorter_connections,
                preferred_airlines=(airline.strip(),) if airline.strip() else (),
                willingness_to_pay_more=willingness,
            ),
            policy=AlertPolicy(
                currency="USD",
                first_observation="suppress",
                minimum_absolute_improvement=threshold,
            ),
            check_interval=timedelta(hours=cadence),
        )
    except (ValidationError, ValueError):
        st.error(
            "Check the trip: use different three-letter airports, a return date "
            "on or after departure, and a positive numeric improvement threshold."
        )
        return None


def show_watch(configuration: ScheduledWatch) -> None:
    watch = configuration.watch
    constraints = configuration.constraints
    st.subheader(f"{watch.origin} → {watch.destination}")
    st.caption(
        f"{watch.outbound_date:%d %b %Y} — {watch.return_date:%d %b %Y} · "
        f"Check interval: {configuration.check_interval}"
    )
    st.text(
        f"Hard limits: {configuration.constraints.max_stops_per_direction} stops; "
        f"{configuration.constraints.max_duration_minutes_per_direction} minutes "
        "per direction"
    )
    st.text(
        f"Connection limit: {constraints.max_connection_duration_minutes} "
        f"minutes · Airport transfers: "
        f"{'allowed' if constraints.allow_airport_transfers else 'excluded'}"
    )
    preferences = configuration.preferences
    st.text(
        f"Preferred stops: {preferences.preferred_max_stops_per_direction} · "
        f"Pay more for convenience: {preferences.willingness_to_pay_more}"
    )
    shorter = preferences.prefer_shorter_total_travel_time
    connections = preferences.prefer_shorter_connections
    st.text(
        f"Shorter travel: {'preferred' if shorter else 'no preference'} · "
        f"Shorter connections: {'preferred' if connections else 'no preference'}"
    )
    if preferences.preferred_airlines:
        st.text("Preferred airlines: " + ", ".join(preferences.preferred_airlines))
    st.text(
        f"Alert threshold: USD {configuration.policy.minimum_absolute_improvement} "
        "improvement; first observation stays silent"
    )


def show_report(report: CheckReport) -> None:
    st.divider()
    st.caption("SYNTHETIC DEMO RESULT" if report.simulated else "LIVE CHECK RESULT")
    show_watch(report.configuration)
    st.caption(f"Checked {report.checked_at:%Y-%m-%d %H:%M UTC}")
    if report.error:
        st.error(report.error)
        return
    result = report.result
    if result is None:
        st.info("No monitoring result is available.")
        return
    decision = result.alert_decision
    if decision is None:
        st.info("Check completed · no eligible round-trip recommendation. No alert.")
    elif decision.should_alert:
        st.success("Worth alerting — FareSentry found a meaningful opportunity")
    else:
        st.info(
            "Completed + silent · No alert — nothing worthwhile enough to interrupt you"
        )
    st.subheader("From search to a considered choice")
    a, b, c = st.columns(3)
    a.metric("Outbound choices", result.outbound_choices_found)
    b.metric("Return lookups", result.return_lookups_attempted)
    c.metric("Completed round trips", result.completed_round_trips)
    a, b, c = st.columns(3)
    a.metric("Outbounds rejected", result.outbound_choices_rejected)
    b.metric("Round trips rejected", result.rejected_by_constraints)
    c.metric("Eligible candidates", result.eligible_candidates)
    st.caption("Hard limits are checked before the agent compares acceptable trips.")
    selected = result.selected_itinerary
    if selected is not None:
        st.subheader("Your selected itinerary")
        st.metric("Round-trip fare", f"{selected.currency} {selected.total_price:,.2f}")
        a, b = st.columns(2)
        for column, label, direction in (
            (a, "Outbound", selected.outbound),
            (b, "Return", selected.inbound),
        ):
            with column:
                st.markdown(f"**{label}**")
                st.text(f"{direction.origin} → {direction.destination}")
                st.text(
                    f"{direction.stops} stops · {direction.duration_minutes} minutes"
                )
                st.text(", ".join(direction.airlines))
                st.text("Flights: " + ", ".join(direction.flight_numbers))
                for connection in direction.connections:
                    st.text(
                        f"Connection: {connection.arrival_airport} → "
                        f"{connection.departure_airport}, "
                        f"{connection.duration_minutes} minutes"
                    )
    if result.recommendation is not None:
        st.subheader("Why the agent chose it")
        if report.simulated:
            st.caption(
                "Scripted recommendation for this demo; no live model was invoked."
            )
        text = safe_recommendation_text(
            result,
            result.recommendation.recommendation,
            current_candidate_ids=getattr(report, "current_candidate_ids", None),
        )
        if text:
            st.text(text)
        for tradeoff in result.recommendation.key_tradeoffs:
            text = safe_recommendation_text(
                result,
                tradeoff.explanation,
                current_candidate_ids=getattr(report, "current_candidate_ids", None),
            )
            if text:
                st.text(f"{tradeoff.category.replace('_', ' ').capitalize()}: {text}")
    if decision is not None:
        st.subheader("Is it worth an interruption?")
        a, b, c = st.columns(3)
        for column, label, value in (
            (
                a,
                "Previous fare · same itinerary",
                decision.previous_same_itinerary_price,
            ),
            (b, "Prior eligible low", decision.prior_watch_low),
            (c, "Price improvement", decision.absolute_improvement),
        ):
            column.metric(
                label,
                f"{decision.currency} {value:,.2f}"
                if value is not None
                else "Not available",
            )
        if decision.percentage_improvement is not None:
            st.caption(f"Improvement: {decision.percentage_improvement}%")
        with st.expander("See the policy checks", expanded=True):
            for signal in decision.signals:
                if signal.role != "information":
                    st.text(f"{'✓' if signal.satisfied else '—'} {signal.explanation}")
    st.subheader("Notification")
    outcome = report.notification
    if outcome is None:
        if decision is not None and decision.should_alert:
            st.info("Alert approved · email has not been requested.")
        else:
            st.caption("No email needed. This check did not approve an alert.")
    elif outcome.status == "failed":
        st.error(
            "Email delivery failed; the monitoring result remains valid. "
            "Inspect the configuration before retrying. A retry after an "
            "uncertain send can duplicate the email."
        )
    elif outcome.status in ("delivered", "already_delivered"):
        if report.simulated:
            st.success(
                "Completed + alerted · Simulated notification delivered. "
                "No real email sent."
            )
        elif outcome.status == "already_delivered":
            st.success("Already delivered · duplicate email suppressed.")
        else:
            st.success("Completed + alerted · SES accepted the email for delivery.")
    else:
        st.caption("No notification needed · the traveler stays undisturbed.")
    st.subheader("Recent fare observations")
    if report.history_error:
        st.warning("The check completed, but recent history could not be loaded.")
    elif report.history:
        st.dataframe(history_rows(report.history), hide_index=True, width="stretch")
        st.caption(
            "Observed candidate fares, not a prediction. Raw history may include "
            "incomplete checks; the alert evaluator excludes those checks."
        )
    else:
        st.caption("No fare observations for this watch yet.")
    if report.due_after_check is not None:
        st.caption(
            "Cadence after this attempt: "
            + ("due" if report.due_after_check else "not due")
            + ". This page runs explicit checks; it does not host a background loop."
        )


def main() -> None:
    st.set_page_config(page_title="FareSentry", page_icon="✈️", layout="wide")
    # The host may override config.toml with its older, less restrictive setting.
    st.set_option("client.showErrorDetails", "none")
    st.title("FareSentry")
    st.markdown("### Your trip. Your tradeoffs. Only the alerts that matter.")
    st.caption(
        "Autonomous airfare monitoring that only interrupts you when it matters."
    )
    modes = ["Demo Mode", "Live Mode"] if live_mode_enabled() else ["Demo Mode"]
    mode = st.radio("Experience", modes, horizontal=True)
    if not live_mode_enabled():
        st.caption(
            "Demo-only experience. Live integrations are available in the local app."
        )
    state_key = "demo_report" if mode == "Demo Mode" else "live_report"
    if mode == "Demo Mode":
        st.info(
            "Synthetic fares · scripted recommendation · simulated email. "
            "No accounts, credentials, or external services required."
        )
        scenario = st.radio(
            "Choose a story",
            ["Routine check · stay silent", "Worthwhile opportunity · alert"],
            horizontal=True,
        )
        st.caption(
            "The same trip was previously USD 1,000. Compare an unchanged fare "
            "with a drop to USD 800. The cheapest two-stop trip fails the hard limit."
        )
        with st.expander("Watch intent · fixed demo preset"):
            show_watch(demo_watch())
        if st.button("Run FareSentry Check", type="primary"):
            with st.spinner("Checking fares and considering the tradeoffs…"):
                try:
                    st.session_state[state_key] = run_demo(
                        "routine" if scenario.startswith("Routine") else "opportunity"
                    )
                except Exception:
                    st.session_state.pop(state_key, None)
                    st.error(
                        "The demo could not complete. "
                        "Check local temporary-directory access."
                    )
    else:
        st.warning(
            "Live checks use SerpApi and Bedrock and may incur charges. "
            "A check never sends email; approved email delivery has a separate button."
        )
        missing = missing_live_configuration()
        if missing:
            st.info(
                "Not ready · set these environment variables before starting: "
                + ", ".join(missing)
            )
        else:
            st.caption(
                "Configuration present. AWS authentication and permissions "
                "are checked on execution."
            )
        configuration = watch_form()
        if configuration is not None:
            with st.spinner("Checking live fares…"):
                st.session_state[state_key] = run_live_check(configuration)
    report = st.session_state.get(state_key)
    if report is not None:
        st.caption(
            "Showing the last submitted check; changing controls does not rerun it."
        )
        if (
            mode == "Live Mode"
            and report.result is not None
            and report.result.alert_decision is not None
            and report.result.alert_decision.should_alert
        ):
            missing = missing_live_configuration(email=True)
            if missing:
                st.info("Email not ready · set: " + ", ".join(missing))
            if st.button(
                "Send this approved alert by real email", disabled=bool(missing)
            ):
                outcome = deliver_live_alert(report)
                report = report.model_copy(update={"notification": outcome})
                st.session_state[state_key] = report
        show_report(report)
    else:
        st.divider()
        st.markdown(
            "**Define intent → Check fares → Reject poor fits → "
            "Consider history → Alert selectively**"
        )
        st.caption("A silent check is a successful outcome. Run a scenario to see why.")


if __name__ == "__main__":
    main()

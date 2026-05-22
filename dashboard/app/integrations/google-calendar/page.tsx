"use client";

import ConnectorDetailPage from "../_connector_detail";


const EXAMPLE = `import lightsei

lightsei.init(api_key=..., agent_name="scheduler")

# List the next week of events
events = lightsei.calendar.list_events(
    time_min="2026-05-21T00:00:00Z",
    time_max="2026-05-28T00:00:00Z",
)

# Create a meeting
lightsei.calendar.create_event(
    summary="Weekly sync",
    start={"dateTime": "2026-05-23T14:00:00-07:00"},
    end={"dateTime": "2026-05-23T14:30:00-07:00"},
    attendees=[{"email": "alice@example.com"}],
)`;


export default function GoogleCalendarDetailPage(): JSX.Element {
  return (
    <ConnectorDetailPage
      connectorType="google_calendar"
      capabilityHint="connector:google_calendar"
      exampleCode={EXAMPLE}
    />
  );
}

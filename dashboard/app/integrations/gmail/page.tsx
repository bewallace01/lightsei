"use client";

import ConnectorDetailPage from "../_connector_detail";


const EXAMPLE = `import lightsei

lightsei.init(api_key=..., agent_name="vega")

# Send mail
lightsei.gmail.send_email(
    to="alice@example.com",
    subject="Daily digest",
    body="...",
)

# Search
results = lightsei.gmail.search_inbox(
    "is:unread label:^t",
    max_results=10,
)`;


export default function GmailDetailPage(): JSX.Element {
  return (
    <ConnectorDetailPage
      connectorType="gmail"
      capabilityHint="connector:gmail"
      exampleCode={EXAMPLE}
    />
  );
}

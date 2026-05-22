"use client";

import ConnectorDetailPage from "../_connector_detail";


const EXAMPLE = `import lightsei

lightsei.init(api_key=..., agent_name="archivist")

# Search workspace files
files = lightsei.drive.list_files(
    query="name contains 'Q3' and trashed = false",
)

# Download (auto-exports Google Docs/Sheets/Slides as text/csv/pdf)
content, mime, name = lightsei.drive.download_file_bytes("FILE_ID")

# Upload
lightsei.drive.upload_file_bytes(
    name="report.txt",
    content=b"Q3 results...",
    mime_type="text/plain",
)`;


export default function GoogleDriveDetailPage(): JSX.Element {
  return (
    <ConnectorDetailPage
      connectorType="google_drive"
      capabilityHint="connector:google_drive"
      exampleCode={EXAMPLE}
    />
  );
}

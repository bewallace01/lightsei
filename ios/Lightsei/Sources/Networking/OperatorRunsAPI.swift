// Phase 30.4.b: operator-side Runs API client.
//
// Two endpoints feed the Runs surface:
//
//   GET /runs?with_summary=true   list rows + per-row summary
//                                 fields (model / tokens / latency /
//                                 event_count / denied / denial),
//                                 server-side aggregated in one
//                                 batched events query (Phase 30.4.a).
//   GET /runs/{id}/events         the full event list for the detail
//                                 view; events carry arbitrary JSONB
//                                 payloads so we model `payload` as
//                                 a JSONValue enum that survives the
//                                 round-trip without loss.
//
// Field names match _serialize_run_event / the Phase 30.4.a /runs
// response in backend/main.py.

import Foundation

struct OperatorRunDenial: Codable, Equatable, Hashable {
    let policy: String?
    let reason: String?
    let cap_usd: Double?
    let cost_so_far_usd: Double?
    let action: String?
}

struct OperatorRun: Codable, Identifiable, Equatable, Hashable {
    let id: String
    let agent_name: String
    let started_at: Date
    let ended_at: Date?
    let triggered_by_trigger_id: String?
    let trigger_kind: String?
    let trigger_name: String?

    // Phase 30.4.a summary fields (only present when caller passed
    // with_summary=true). Optional so the default-shape response
    // decodes too.
    let model: String?
    let input_tokens: Int?
    let output_tokens: Int?
    let latency_ms: Int?
    let event_count: Int?
    let denied: Bool?
    let denial: OperatorRunDenial?
}

struct OperatorRunsListResponse: Codable {
    let runs: [OperatorRun]
}

// /runs/{id}/events row. The payload is arbitrary JSON; JSONValue
// preserves shape + values so the detail view can either render
// raw or pull selected fields out.
struct OperatorRunEvent: Codable, Identifiable, Equatable, Hashable {
    let id: Int
    let run_id: String
    let agent_name: String
    let kind: String
    let payload: JSONValue
    let timestamp: Date
}

// Minimal snapshot of the run row, returned alongside the events.
// The detail view uses this for its header (agent name + timestamps)
// rather than re-fetching the full /runs row.
struct OperatorRunSnapshot: Codable, Equatable, Hashable {
    let id: String
    let agent_name: String
    let started_at: Date
    let ended_at: Date?
}

struct OperatorRunEventsResponse: Codable {
    let run: OperatorRunSnapshot
    let events: [OperatorRunEvent]
}

// MARK: JSONValue

/// Minimal JSON-shaped value type so arbitrary JSONB payloads from
/// the backend round-trip through Codable without us having to
/// declare every possible event shape up front. Display code can
/// switch on the case (or just call `prettyPrinted` for the detail
/// view).
enum JSONValue: Codable, Equatable, Hashable {
    case null
    case bool(Bool)
    case number(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() {
            self = .null
        } else if let v = try? c.decode(Bool.self) {
            self = .bool(v)
        } else if let v = try? c.decode(Double.self) {
            self = .number(v)
        } else if let v = try? c.decode(String.self) {
            self = .string(v)
        } else if let v = try? c.decode([JSONValue].self) {
            self = .array(v)
        } else if let v = try? c.decode([String: JSONValue].self) {
            self = .object(v)
        } else {
            throw DecodingError.dataCorruptedError(
                in: c,
                debugDescription: "unrecognized JSON value",
            )
        }
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .null: try c.encodeNil()
        case .bool(let v): try c.encode(v)
        case .number(let v): try c.encode(v)
        case .string(let v): try c.encode(v)
        case .array(let v): try c.encode(v)
        case .object(let v): try c.encode(v)
        }
    }

    /// Pretty-printed JSON for the run-detail view's raw payload
    /// dump. Returns "{}" on serialization failure so the view never
    /// shows nothing.
    var prettyPrinted: String {
        // Round-trip through JSONSerialization to get system-quality
        // formatting (sorted keys + nested indent).
        guard let data = try? JSONEncoder().encode(self),
              let any = try? JSONSerialization.jsonObject(
                  with: data, options: [.fragmentsAllowed],
              ),
              let pretty = try? JSONSerialization.data(
                  withJSONObject: any,
                  options: [.prettyPrinted, .sortedKeys],
              ),
              let s = String(data: pretty, encoding: .utf8) else {
            return "{}"
        }
        return s
    }
}

// MARK: APIClient extensions

extension APIClient {
    /// List runs in the active workspace, newest first. Always asks
    /// for the with_summary shape — the basic /runs response would
    /// force the mobile to fall back to per-run events fetches.
    func fetchOperatorRuns(
        triggerID: String? = nil, limit: Int = 50,
    ) async throws -> [OperatorRun] {
        var qs = "with_summary=true&limit=\(limit)"
        if let triggerID, !triggerID.isEmpty {
            let encoded = triggerID.addingPercentEncoding(
                withAllowedCharacters: .urlQueryAllowed,
            ) ?? triggerID
            qs += "&trigger_id=\(encoded)"
        }
        let resp: OperatorRunsListResponse = try await request(
            "runs?\(qs)",
        )
        return resp.runs
    }

    /// Returns the events alone (caller already has the run summary
    /// from the list). For the run-detail header use
    /// `fetchOperatorRunWithEvents` which returns the snapshot too.
    func fetchOperatorRunEvents(
        runID: String,
    ) async throws -> [OperatorRunEvent] {
        let resp: OperatorRunEventsResponse = try await request(
            "runs/\(runID)/events",
        )
        return resp.events
    }

    func fetchOperatorRunWithEvents(
        runID: String,
    ) async throws -> OperatorRunEventsResponse {
        try await request("runs/\(runID)/events")
    }
}

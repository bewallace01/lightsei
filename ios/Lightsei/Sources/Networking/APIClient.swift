// Phase 29.2a: thin URLSession wrapper for the Lightsei backend.
//
// Thin client per the Phase 29 spec: no business logic, every call
// hits the existing backend endpoints. Keeps types narrow to what
// each endpoint actually returns so the iOS surface gets a compile
// error if the web /c surface and the iOS app drift on response
// shape.

import Foundation

enum APIError: Error, LocalizedError {
    case badStatus(Int, String?)
    case decode(Error)
    case transport(Error)
    case unauthorized

    var errorDescription: String? {
        switch self {
        case .badStatus(let code, let body):
            if let body, !body.isEmpty { return body }
            return "Request failed (HTTP \(code))."
        case .decode(let e): return "Couldn't read response: \(e.localizedDescription)"
        case .transport(let e): return e.localizedDescription
        case .unauthorized: return "Sign-in expired. Please sign in again."
        }
    }
}

struct APIClient {
    let baseURL: URL
    var session: URLSession = .shared
    // Optional bearer for endpoints behind end-user auth. nil for
    // unauthenticated requests like the magic-link request.
    var bearer: String? = nil

    static var production: APIClient {
        APIClient(baseURL: URL(string: "https://api.lightsei.com")!)
    }

    func request<Out: Decodable>(
        _ path: String,
        method: String = "GET",
        body: Encodable? = nil,
        as: Out.Type = Out.self,
    ) async throws -> Out {
        var req = URLRequest(url: baseURL.appendingPathComponent(path))
        req.httpMethod = method
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        if let bearer {
            req.setValue("Bearer \(bearer)", forHTTPHeaderField: "Authorization")
        }
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONEncoder().encode(AnyEncodable(body))
        }
        let (data, resp): (Data, URLResponse)
        do {
            (data, resp) = try await session.data(for: req)
        } catch {
            throw APIError.transport(error)
        }
        guard let http = resp as? HTTPURLResponse else {
            throw APIError.badStatus(0, nil)
        }
        if http.statusCode == 401 {
            throw APIError.unauthorized
        }
        if !(200...299).contains(http.statusCode) {
            // Try to surface the structured detail.message the
            // backend returns; fall through to raw body on shape
            // mismatch.
            let msg = (try? JSONDecoder().decode(StructuredError.self, from: data))
                .flatMap { ($0.detail as? StructuredErrorDetail)?.message ?? $0.detail as? String }
                ?? String(data: data, encoding: .utf8)
            throw APIError.badStatus(http.statusCode, msg)
        }
        do {
            return try JSONDecoder.lightsei.decode(Out.self, from: data)
        } catch {
            throw APIError.decode(error)
        }
    }
}

private struct StructuredError: Decodable {
    let detail: Any?
    private enum CodingKeys: String, CodingKey { case detail }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        if let s = try? c.decode(String.self, forKey: .detail) {
            self.detail = s
        } else if let d = try? c.decode(StructuredErrorDetail.self, forKey: .detail) {
            self.detail = d
        } else {
            self.detail = nil
        }
    }
}

private struct StructuredErrorDetail: Decodable {
    let error: String?
    let message: String?
}

private struct AnyEncodable: Encodable {
    let wrapped: Encodable
    init(_ wrapped: Encodable) { self.wrapped = wrapped }
    func encode(to encoder: Encoder) throws {
        try wrapped.encode(to: encoder)
    }
}

extension JSONDecoder {
    static var lightsei: JSONDecoder {
        let d = JSONDecoder()
        d.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            let raw = try container.decode(String.self)
            if let date = decodeLightseiDate(raw) {
                return date
            }
            throw DecodingError.dataCorruptedError(
                in: container,
                debugDescription: "Invalid ISO8601 date: \(raw)",
            )
        }
        return d
    }

    private static func decodeLightseiDate(_ raw: String) -> Date? {
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [
            .withInternetDateTime,
            .withFractionalSeconds,
        ]
        if let date = fractional.date(from: raw) {
            return date
        }

        let standard = ISO8601DateFormatter()
        standard.formatOptions = [.withInternetDateTime]
        return standard.date(from: raw)
    }
}

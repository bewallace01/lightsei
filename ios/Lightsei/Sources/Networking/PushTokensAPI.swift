// Phase 29.4 stub: typed wrappers for the APNS device-token endpoints.
//
// PushRegistration calls registerAPNSToken on the AuthStore's
// authed APIClient after iOS hands the device token to the
// AppDelegate. unregisterAPNSToken is used on sign-out or when
// the user disables notifications in iOS settings.

import Foundation

struct ApnsRegisterRequest: Encodable {
    let device_token: String
    let bundle_id: String
    let environment: String   // "sandbox" or "production"
}

struct ApnsUnregisterRequest: Encodable {
    let device_token: String
}

struct ApnsRegisterResponse: Codable {
    let id: String
    let device_token: String
    let active: Bool
}

struct ApnsUnregisterResponse: Codable {
    let revoked: Bool
    let device_token: String
}

extension APIClient {
    func registerAPNSToken(
        deviceToken: String,
        bundleID: String,
        environment: String,
    ) async throws -> ApnsRegisterResponse {
        try await request(
            "me/end-user/apns-tokens",
            method: "POST",
            body: ApnsRegisterRequest(
                device_token: deviceToken,
                bundle_id: bundleID,
                environment: environment,
            ),
        )
    }

    func unregisterAPNSToken(
        deviceToken: String,
    ) async throws -> ApnsUnregisterResponse {
        try await request(
            "me/end-user/apns-tokens",
            method: "DELETE",
            body: ApnsUnregisterRequest(device_token: deviceToken),
        )
    }
}

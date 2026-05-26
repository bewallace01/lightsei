// Phase 29.2a: Keychain wrapper for the end-user session token.
//
// Session tokens live in the Keychain instead of UserDefaults so
// they survive app uninstall + reinstall (intentional in Apple
// docs — kSecAttrAccessibleAfterFirstUnlock makes the token
// available once the user has unlocked the device since boot, but
// not while the device is locked at the lock screen).
//
// One service + account per app. Add additional accounts in 29.4+
// if we ever need multi-account storage on a single device.

import Foundation
import Security

enum KeychainError: Error {
    case unhandled(OSStatus)
}

struct Keychain {
    static let service = "com.lightsei.app.session"
    static let account = "end-user"

    static func read() -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        guard status == errSecSuccess,
              let data = item as? Data,
              let s = String(data: data, encoding: .utf8) else {
            return nil
        }
        return s
    }

    static func write(_ token: String) throws {
        let data = Data(token.utf8)
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        let attrs: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String:
                kSecAttrAccessibleAfterFirstUnlock,
        ]
        let status = SecItemUpdate(
            query as CFDictionary, attrs as CFDictionary,
        )
        if status == errSecItemNotFound {
            var addQuery = query
            addQuery[kSecValueData as String] = data
            addQuery[kSecAttrAccessible as String] =
                kSecAttrAccessibleAfterFirstUnlock
            let addStatus = SecItemAdd(addQuery as CFDictionary, nil)
            if addStatus != errSecSuccess {
                throw KeychainError.unhandled(addStatus)
            }
        } else if status != errSecSuccess {
            throw KeychainError.unhandled(status)
        }
    }

    static func clear() {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        _ = SecItemDelete(query as CFDictionary)
    }
}

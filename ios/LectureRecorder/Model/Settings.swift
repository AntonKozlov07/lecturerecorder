import Foundation
import Observation
import Security

/// Secrets live in the Keychain; everything else in UserDefaults.
enum Keychain {
    static let service = "app.lecturerecorder"

    static func get(_ key: String) -> String {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
                                    kSecAttrAccount as String: key, kSecReturnData as String: true,
                                    kSecMatchLimit as String: kSecMatchLimitOne]
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess, let data = item as? Data else { return "" }
        return String(decoding: data, as: UTF8.self)
    }

    static func set(_ key: String, _ value: String) {
        let base: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
                                   kSecAttrAccount as String: key]
        SecItemDelete(base as CFDictionary)
        guard !value.isEmpty else { return }
        var add = base
        add[kSecValueData as String] = Data(value.utf8)
        add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        SecItemAdd(add as CFDictionary, nil)
    }
}

@MainActor
@Observable
final class AppSettings {
    static let models: [(id: String, label: String)] = [
        ("claude-opus-5", "Claude Opus 5 (best quality)"),
        ("claude-sonnet-5", "Claude Sonnet 5 (faster, cheaper)"),
        ("claude-haiku-4-5", "Claude Haiku 4.5 (fastest, cheapest)"),
    ]

    var apiKey: String { didSet { Keychain.set("anthropic_api_key", apiKey) } }
    var githubToken: String { didSet { Keychain.set("github_token", githubToken) } }
    var githubRepo: String { didSet { defaults.set(githubRepo, forKey: "github_repo") } }
    var model: String { didSet { defaults.set(model, forKey: "model") } }
    var autoNotes: Bool { didSet { defaults.set(autoNotes, forKey: "auto_notes") } }
    var filterSideTalk: Bool { didSet { defaults.set(filterSideTalk, forKey: "filter_side_talk") } }
    var language: String { didSet { defaults.set(language, forKey: "language") } }

    private let defaults: UserDefaults

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        apiKey = Keychain.get("anthropic_api_key")
        githubToken = Keychain.get("github_token")
        githubRepo = defaults.string(forKey: "github_repo") ?? ""
        model = defaults.string(forKey: "model") ?? "claude-opus-5"
        autoNotes = defaults.object(forKey: "auto_notes") as? Bool ?? true
        filterSideTalk = defaults.object(forKey: "filter_side_talk") as? Bool ?? true
        language = defaults.string(forKey: "language") ?? ""
    }

    var aiReady: Bool { !apiKey.trimmingCharacters(in: .whitespaces).isEmpty }
    var syncConfigured: Bool { !githubRepo.isEmpty && !githubToken.isEmpty }

    /// Accept "owner/name" or a pasted GitHub URL.
    static func normalizeRepo(_ value: String) -> String {
        var s = value.trimmingCharacters(in: .whitespacesAndNewlines)
        for prefix in ["https://", "http://", "www.", "github.com/"] where s.lowercased().hasPrefix(prefix) {
            s.removeFirst(prefix.count)
        }
        if s.lowercased().hasPrefix("github.com/") { s.removeFirst("github.com/".count) }
        if s.hasSuffix(".git") { s.removeLast(4) }
        return s.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
    }
}

import Foundation

/// Minimal Claude Messages API client over HTTP (there is no official Swift SDK).
/// Streams text for notes and chat, and returns JSON for structured requests.
struct ClaudeClient {
    var apiKey: String
    var model: String
    var baseURL = URL(string: "https://api.anthropic.com/v1/messages")!

    enum ClaudeError: LocalizedError {
        case message(String)
        var errorDescription: String? { if case .message(let m) = self { return m }; return nil }
    }

    private func request(body: [String: Any], betas: [String]) throws -> URLRequest {
        var req = URLRequest(url: baseURL)
        req.httpMethod = "POST"
        req.timeoutInterval = 600
        req.setValue("application/json", forHTTPHeaderField: "content-type")
        req.setValue(apiKey, forHTTPHeaderField: "x-api-key")
        req.setValue("2023-06-01", forHTTPHeaderField: "anthropic-version")
        if !betas.isEmpty { req.setValue(betas.joined(separator: ","), forHTTPHeaderField: "anthropic-beta") }
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        return req
    }

    /// Model-specific options: adaptive thinking with an effort level (not on Haiku), and on
    /// Claude Opus 5 the server-side fallback, so a declined request is retried on another model.
    private func options(effort: String, into body: inout [String: Any]) -> [String] {
        body["model"] = model
        if !model.hasPrefix("claude-haiku") {
            body["thinking"] = ["type": "adaptive"]
            var config = body["output_config"] as? [String: Any] ?? [:]
            config["effort"] = effort
            body["output_config"] = config
        }
        if model == "claude-opus-5" {
            body["fallbacks"] = "default"
            return ["server-side-fallback-2026-07-01"]
        }
        return []
    }

    static func friendly(status: Int, body: Data) -> String {
        let detail = (try? JSONSerialization.jsonObject(with: body) as? [String: Any])
            .flatMap { ($0["error"] as? [String: Any])?["message"] as? String } ?? ""
        switch status {
        case 401: return "The API key was rejected. Check it in Settings."
        case 403: return "This API key doesn't have access to the selected model."
        case 404: return "The selected model wasn't found. Pick another one in Settings."
        case 413: return "There's too much material for one request. Leave some out and try again."
        case 429: return "Rate limited by the API. Wait a moment and try again."
        default: return "API error (\(status)): \(detail)"
        }
    }

    /// Stream the reply's text as it's generated.
    func stream(system: String, messages: [[String: Any]], effort: String) -> AsyncThrowingStream<String, Error> {
        var body: [String: Any] = ["max_tokens": 64000, "stream": true, "messages": messages]
        if !system.isEmpty { body["system"] = system }
        let betas = options(effort: effort, into: &body)
        return AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    let (bytes, response) = try await URLSession.shared.bytes(for: request(body: body, betas: betas))
                    let status = (response as? HTTPURLResponse)?.statusCode ?? 0
                    if status != 200 {
                        var data = Data()
                        for try await byte in bytes { data.append(byte) }
                        throw ClaudeError.message(Self.friendly(status: status, body: data))
                    }
                    var stopReason = ""
                    for try await line in bytes.lines {
                        guard line.hasPrefix("data: "), let data = line.dropFirst(6).data(using: .utf8),
                              let event = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { continue }
                        switch event["type"] as? String {
                        case "content_block_delta":
                            if let delta = event["delta"] as? [String: Any], delta["type"] as? String == "text_delta",
                               let text = delta["text"] as? String {
                                continuation.yield(text)
                            }
                        case "message_delta":
                            stopReason = ((event["delta"] as? [String: Any])?["stop_reason"] as? String) ?? stopReason
                        case "error":
                            let message = ((event["error"] as? [String: Any])?["message"] as? String) ?? "The API returned an error."
                            throw ClaudeError.message(message)
                        default:
                            break
                        }
                    }
                    if stopReason == "refusal" { throw ClaudeError.message("The model declined this request.") }
                    if stopReason == "max_tokens" { continuation.yield("\n\n*(Response was cut off because it reached the length limit.)*") }
                    continuation.finish()
                } catch is CancellationError {
                    continuation.finish()
                } catch let error as URLError {
                    continuation.finish(throwing: ClaudeError.message(
                        error.code == .notConnectedToInternet ? "You're offline. AI features need an internet connection."
                            : "Couldn't reach the Anthropic API (\(error.localizedDescription))."))
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    /// One request whose reply must match `schema`; returns the decoded JSON object.
    func structured(system: String, messages: [[String: Any]], schema: [String: Any], effort: String,
                    model overrideModel: String? = nil) async throws -> [String: Any] {
        var body: [String: Any] = ["max_tokens": 16000, "messages": messages,
                                   "output_config": ["format": ["type": "json_schema", "schema": schema]]]
        if !system.isEmpty { body["system"] = system }
        var client = self
        if let overrideModel { client.model = overrideModel }
        let betas = client.options(effort: effort, into: &body)
        let data: Data, response: URLResponse
        do {
            (data, response) = try await URLSession.shared.data(for: request(body: body, betas: betas))
        } catch let error as URLError {
            throw ClaudeError.message(error.code == .notConnectedToInternet
                ? "You're offline. AI features need an internet connection." : "Couldn't reach the Anthropic API.")
        }
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard status == 200 else { throw ClaudeError.message(Self.friendly(status: status, body: data)) }
        guard let message = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw ClaudeError.message("The API returned something unexpected.")
        }
        switch message["stop_reason"] as? String {
        case "refusal": throw ClaudeError.message("The model declined this request.")
        case "max_tokens": throw ClaudeError.message("The response was too long. Try asking for fewer items.")
        default: break
        }
        let text = (message["content"] as? [[String: Any]] ?? [])
            .filter { $0["type"] as? String == "text" }.compactMap { $0["text"] as? String }.joined()
        guard let json = try? JSONSerialization.jsonObject(with: Data(text.utf8)) as? [String: Any] else {
            throw ClaudeError.message("The model returned malformed data. Try again.")
        }
        return json
    }
}

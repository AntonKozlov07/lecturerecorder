import Foundation

// The documents below are exactly what's stored in the sync repository
// (see docs/sync-format.md). The phone keeps them as JSON files on disk too,
// so syncing is a matter of comparing and copying files.

func newID() -> String {
    String(UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased().prefix(12))
}

func now() -> Double { Date().timeIntervalSince1970 }

struct TranscriptLine: Codable, Hashable {
    var t: Double
    var end: Double
    var text: String
    var sideTalk: Bool = false
}

struct Topic: Codable, Hashable {
    var title: String
    var summary: String
    var timestamp: String
}

struct DeepDive: Codable, Hashable {
    var topic: String
    var content: String
    var createdAt: Double
}

struct ChatMessage: Codable, Hashable, Identifiable {
    var role: String
    var content: String
    var createdAt: Double
    var id: String { "\(createdAt)-\(role)" }
}

struct QuizQuestion: Codable, Hashable {
    var question: String
    // Multiple choice
    var options: [String]?
    var answerIndex: Int?
    var explanation: String?
    // Written answer
    var modelAnswer: String?
    var keyPoints: [String]?
    var source: String?
}

/// A quiz answer: a chosen option, a written answer, or nothing.
enum Answer: Codable, Hashable {
    case choice(Int)
    case text(String)
    case none

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .none }
        else if let i = try? c.decode(Int.self) { self = .choice(i) }
        else { self = .text(try c.decode(String.self)) }
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .choice(let i): try c.encode(i)
        case .text(let s): try c.encode(s)
        case .none: try c.encodeNil()
        }
    }

    var isAnswered: Bool {
        switch self {
        case .choice: return true
        case .text(let s): return !s.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        case .none: return false
        }
    }
}

struct Grade: Codable, Hashable {
    var verdict: String
    var feedback: String
}

struct Quiz: Codable, Hashable, Identifiable {
    var createdAt: Double
    var kind: String = "choice"
    var difficulty: String = "medium"
    var questions: [QuizQuestion]
    var answers: [Answer]?
    var grading: [Grade]?
    var score: Double?
    var id: Double { createdAt }
}

struct Flashcard: Codable, Hashable {
    var front: String
    var back: String
    var source: String? = ""
}

struct LectureDoc: Codable, Hashable, Identifiable {
    var format: Int = 1
    var id: String
    var title: String
    var course: String = ""
    var createdAt: Double
    var modifiedAt: Double?
    var duration: Double = 0
    var transcript: [TranscriptLine] = []
    var sideTalkChecked: Bool = false
    var notes: String = ""
    var topics: [Topic] = []
    var deepDives: [DeepDive] = []
    var chat: [ChatMessage] = []
    var quizzes: [Quiz] = []
    var flashcards: [Flashcard] = []

    /// The transcript the AI reads: timestamps on, side talk left out.
    var aiTranscript: String {
        transcript.filter { !$0.sideTalk }.map { "[\(formatTimestamp($0.t))] \($0.text)" }.joined(separator: "\n")
    }
}

struct Material: Codable, Hashable, Identifiable {
    var id: String
    var filename: String
    var kind: String          // pdf | pdf_scan | slides | document | text | image
    var pages: Int = 0
    var size: Int = 0
    var createdAt: Double
    var text: String = ""
    var file: String?
}

struct CourseDoc: Codable, Hashable, Identifiable {
    var format: Int = 1
    var id: String
    var name: String
    var createdAt: Double
    var modifiedAt: Double?
    var excluded: [String] = []
    var materials: [Material] = []
    var chat: [ChatMessage] = []
    var quizzes: [Quiz] = []
    var flashcards: [Flashcard] = []
}

struct Tombstones: Codable, Equatable {
    var lectures: [String: Double] = [:]
    var courses: [String: Double] = [:]
    var materials: [String: Double] = [:]
}

func formatTimestamp(_ seconds: Double) -> String {
    let s = max(0, Int(seconds))
    let h = s / 3600, m = (s % 3600) / 60, sec = s % 60
    return h > 0 ? String(format: "%d:%02d:%02d", h, m, sec) : String(format: "%02d:%02d", m, sec)
}

func formatDuration(_ seconds: Double) -> String {
    let s = Int(seconds.rounded())
    if s < 60 { return "\(s)s" }
    let h = s / 3600, m = Int((Double(s % 3600) / 60).rounded())
    return h > 0 ? "\(h)h \(m)m" : "\(m) min"
}

// JSON coding matching the sync format: snake_case keys, sorted for stable output.
enum DocCoding {
    static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    static let encoder: JSONEncoder = {
        let e = JSONEncoder()
        e.keyEncodingStrategy = .convertToSnakeCase
        e.outputFormatting = [.sortedKeys, .prettyPrinted, .withoutEscapingSlashes]
        return e
    }()

    /// Encoder for hashing: compact and sorted.
    static let hashEncoder: JSONEncoder = {
        let e = JSONEncoder()
        e.keyEncodingStrategy = .convertToSnakeCase
        e.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        return e
    }()
}

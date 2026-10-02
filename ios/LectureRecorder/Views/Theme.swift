import SwiftUI
import UIKit

/// Colors shared with the PC app: warm paper surfaces, ink text, one green accent and a muted
/// color per course. Floating controls use Liquid Glass; reading surfaces stay solid.
enum Theme {
    static func dynamic(_ light: UInt32, _ dark: UInt32) -> Color {
        Color(UIColor { $0.userInterfaceStyle == .dark ? UIColor(hex: dark) : UIColor(hex: light) })
    }

    static let paper = dynamic(0xF6F5F1, 0x161615)
    static let surface = dynamic(0xFFFFFF, 0x1E1E1C)
    static let ink = dynamic(0x1D1D1B, 0xE7E5DF)
    static let muted = dynamic(0x6B6963, 0xA29F96)
    static let faint = dynamic(0x9A978F, 0x75736C)
    static let border = dynamic(0xE1DED6, 0x302F2B)
    static let accent = dynamic(0x1F5F4A, 0x7DBB9F)
    static let accentSoft = dynamic(0xE2EDE8, 0x22332C)
    static let record = Color(red: 0.82, green: 0.23, blue: 0.19)
    static let ok = dynamic(0x2E6B3A, 0x86C190)
    static let warn = dynamic(0x8A5A12, 0xE0B56A)
    static let warnSoft = dynamic(0xF6ECD9, 0x3A2F1C)
    static let danger = dynamic(0xB3261E, 0xE5675D)

    private static let courseColors: [(UInt32, UInt32)] = [
        (0x4F7D63, 0x7FB094), (0x4F6F94, 0x82A3C9), (0xB0643F, 0xD58D69), (0xA8832C, 0xCFAE5F),
        (0x3B8583, 0x6DB5B2), (0xA9566A, 0xD08597), (0x71803A, 0xA3B16A), (0x6D6A64, 0xA09C94),
    ]

    /// The library whose courses share out the colors; set by AppModel.
    @MainActor static weak var library: LibraryStore?

    /// Same rules as the PC app (courseColor in app.js), so a course has the same color on both devices.
    @MainActor static func courseColor(_ id: String?) -> Color {
        guard let id, !id.isEmpty else { return faint }
        let index = library.map { assignColors(Array($0.courses.values))[id] } ?? nil
        let (light, dark) = courseColors[index ?? courseColorIndex(id)]
        return dynamic(light, dark)
    }

    /// A course's preferred color: FNV-1a over the id's UTF-16 code units, then a bit mix.
    static func courseColorIndex(_ id: String) -> Int {
        var h: UInt32 = 0x811C9DC5
        for unit in id.utf16 { h ^= UInt32(unit); h = h &* 0x01000193 }
        h ^= h >> 16; h = h &* 0x85EBCA6B
        h ^= h >> 13; h = h &* 0xC2B2AE35
        h ^= h >> 16
        return Int(h % UInt32(courseColors.count))
    }

    /// Hands colors out oldest course first, skipping ones already taken, so up to 8 courses never share a color.
    static func assignColors(_ courses: [CourseDoc]) -> [String: Int] {
        let n = courseColors.count
        let order = courses.sorted { $0.createdAt != $1.createdAt ? $0.createdAt < $1.createdAt : $0.id < $1.id }
        var used = Set<Int>()
        var colors: [String: Int] = [:]
        for course in order {
            let start = courseColorIndex(course.id)
            let pick = (0..<n).map { (start + $0) % n }.first { !used.contains($0) } ?? start
            used.insert(pick)
            colors[course.id] = pick
        }
        return colors
    }
}

extension UIColor {
    convenience init(hex: UInt32) {
        self.init(red: CGFloat((hex >> 16) & 0xFF) / 255, green: CGFloat((hex >> 8) & 0xFF) / 255,
                  blue: CGFloat(hex & 0xFF) / 255, alpha: 1)
    }
}

/// A solid card for reading content.
struct Sheet<Content: View>: View {
    @ViewBuilder var content: Content
    var body: some View {
        content
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Theme.surface, in: .rect(cornerRadius: 16))
            .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Theme.border))
    }
}

struct CourseChip: View {
    var id: String?
    var size: CGFloat = 9
    var body: some View {
        RoundedRectangle(cornerRadius: size / 3).fill(Theme.courseColor(id)).frame(width: size, height: size)
    }
}

/// A one-line pointer to Settings, shown under AI buttons while no API key is saved.
struct NeedsKeyHint: View {
    @Environment(AppModel.self) private var model
    var body: some View {
        if !model.settings.aiReady {
            Label("Add your Anthropic API key in Settings to use this.", systemImage: "key")
                .font(.footnote).foregroundStyle(Theme.muted)
                .frame(maxWidth: .infinity, alignment: .center)
        }
    }
}

struct EmptyHint: View {
    var icon: String
    var title: String
    var message: String
    var body: some View {
        VStack(spacing: 10) {
            Image(systemName: icon).font(.system(size: 30, weight: .light)).foregroundStyle(Theme.faint)
            Text(title).font(.headline)
            Text(message).font(.subheadline).foregroundStyle(Theme.muted).multilineTextAlignment(.center)
        }
        .padding(28)
        .frame(maxWidth: .infinity)
    }
}

/// "Thinking…" with animated dots, shown before the first words of an AI reply arrive.
struct ThinkingLabel: View {
    var text = "Thinking"
    var body: some View {
        TimelineView(.periodic(from: .now, by: 0.4)) { ctx in
            let dots = Int(ctx.date.timeIntervalSinceReferenceDate / 0.4) % 4
            Text(text + String(repeating: ".", count: dots)).foregroundStyle(Theme.muted)
        }
    }
}

extension View {
    /// The app's standard background.
    func paperBackground() -> some View { background(Theme.paper.ignoresSafeArea()) }
}

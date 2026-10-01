import MarkdownUI
import SwiftUI

/// Renders AI-written Markdown. Timestamps like [12:03] become links that play the recording,
/// and LaTeX formulas are converted to Unicode text.
struct MarkdownText: View {
    var text: String
    var size: CGFloat = 16
    var onTimestamp: ((Double) -> Void)?

    var body: some View {
        Markdown(Self.prepare(text, links: onTimestamp != nil))
            .markdownTheme(.lecture(size: size))
            .textSelection(.enabled)
            .environment(\.openURL, OpenURLAction { url in
                if url.scheme == "lecturerecorder", let seconds = Double(url.lastPathComponent) {
                    onTimestamp?(seconds)
                    return .handled
                }
                return .systemAction
            })
    }

    static func prepare(_ text: String, links: Bool) -> String {
        // There's no math renderer on the phone, so LaTeX becomes readable Unicode (ΔS ≥ 0, mv², 1/2).
        var s = LaTeX.render(in: text, markdown: true)
        if links {
            s = s.replacingOccurrences(of: #"\[(\d{1,2}):(\d{2})\](?!\()"#, with: "[$1:$2](lecturerecorder://t/$1:$2)",
                                       options: .regularExpression)
            // Convert the mm:ss in those links into seconds.
            let pattern = try! NSRegularExpression(pattern: #"lecturerecorder://t/(\d{1,2}):(\d{2})"#)
            let ns = s as NSString
            var out = ""
            var last = 0
            for m in pattern.matches(in: s, range: NSRange(location: 0, length: ns.length)) {
                out += ns.substring(with: NSRange(location: last, length: m.range.location - last))
                let mm = Int(ns.substring(with: m.range(at: 1))) ?? 0
                let ss = Int(ns.substring(with: m.range(at: 2))) ?? 0
                out += "lecturerecorder://t/\(mm * 60 + ss)"
                last = m.range.location + m.range.length
            }
            s = out + ns.substring(from: last)
        }
        return s
    }
}

extension MarkdownUI.Theme {
    static func lecture(size: CGFloat) -> MarkdownUI.Theme {
        MarkdownUI.Theme()
            .text {
                ForegroundColor(Theme.ink)
                FontSize(size)
            }
            .link { ForegroundColor(Theme.accent) }
            .code {
                FontFamilyVariant(.monospaced)
                FontSize(.em(0.88))
                BackgroundColor(Theme.border.opacity(0.6))
            }
            .heading1 { configuration in
                configuration.label
                    .markdownMargin(top: 4, bottom: 10)
                    .markdownTextStyle { FontWeight(.bold); FontSize(.em(1.45)) }
            }
            .heading2 { configuration in
                VStack(alignment: .leading, spacing: 6) {
                    configuration.label.markdownTextStyle { FontWeight(.semibold); FontSize(.em(1.18)) }
                    Divider()
                }
                .markdownMargin(top: 20, bottom: 8)
            }
            .heading3 { configuration in
                configuration.label
                    .markdownMargin(top: 14, bottom: 6)
                    .markdownTextStyle { FontWeight(.semibold); FontSize(.em(1.05)) }
            }
            .paragraph { configuration in
                configuration.label.relativeLineSpacing(.em(0.2)).markdownMargin(top: 0, bottom: 10)
            }
            .listItem { configuration in configuration.label.markdownMargin(top: .em(0.2)) }
            .codeBlock { configuration in
                ScrollView(.horizontal) {
                    configuration.label.markdownTextStyle { FontFamilyVariant(.monospaced); FontSize(.em(0.85)) }.padding(12)
                }
                .background(Theme.border.opacity(0.4), in: .rect(cornerRadius: 10))
                .markdownMargin(top: 4, bottom: 12)
            }
            .table { configuration in
                configuration.label
                    .markdownTableBorderStyle(.init(color: Theme.border))
                    .markdownTableBackgroundStyle(.alternatingRows(Theme.surface, Theme.paper))
                    .markdownMargin(top: 4, bottom: 12)
            }
            .tableCell { configuration in
                configuration.label.markdownTextStyle { FontSize(.em(0.9)) }.padding(.horizontal, 8).padding(.vertical, 6)
            }
            .blockquote { configuration in
                configuration.label
                    .markdownTextStyle { ForegroundColor(Theme.muted) }
                    .padding(.leading, 12)
                    .overlay(alignment: .leading) { Rectangle().fill(Theme.border).frame(width: 3) }
            }
    }
}

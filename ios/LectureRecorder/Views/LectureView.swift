import SwiftUI

struct LectureView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    let lectureID: String
    @State private var section: Section = .transcript
    @State private var renaming = false
    @State private var newTitle = ""

    enum Section: String, CaseIterable, Identifiable {
        case transcript = "Transcript", notes = "Notes", chat = "Chat", quiz = "Quiz", cards = "Cards"
        var id: String { rawValue }
        var icon: String {
            switch self {
            case .transcript: return "text.alignleft"
            case .notes: return "doc.text"
            case .chat: return "bubble.left.and.bubble.right"
            case .quiz: return "checklist"
            case .cards: return "rectangle.on.rectangle"
            }
        }
    }

    var body: some View {
        if let lecture = model.store.lecture(lectureID) {
            VStack(spacing: 0) {
                content(lecture)
            }
            .safeAreaInset(edge: .top, spacing: 0) {
                Picker("Section", selection: $section) {
                    ForEach(Section.allCases) { s in Image(systemName: s.icon).accessibilityLabel(s.rawValue).tag(s) }
                }
                .pickerStyle(.segmented)
                .padding(.horizontal, 16).padding(.vertical, 8)
                .glassEffect(.regular, in: .capsule)
                .padding(.horizontal, 12).padding(.bottom, 6)
                .accessibilityIdentifier("sections")
            }
            .paperBackground()
            .navigationTitle(section == .transcript ? lecture.title : section.rawValue)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Menu {
                        Button("Rename", systemImage: "pencil") { newTitle = lecture.title; renaming = true }
                        ShareLink(item: exportText(lecture), preview: SharePreview(lecture.title)) {
                            Label("Share notes and transcript", systemImage: "square.and.arrow.up")
                        }
                        Button("Delete", systemImage: "trash", role: .destructive) {
                            model.store.deleteLecture(lectureID)
                            model.syncSoon()
                            dismiss()
                        }
                    } label: { Image(systemName: "ellipsis") }
                }
            }
            .alert("Rename lecture", isPresented: $renaming) {
                TextField("Title", text: $newTitle)
                Button("Save") { model.store.update(lectureID) { $0.title = newTitle }; model.syncSoon() }
                Button("Cancel", role: .cancel) {}
            }
        } else {
            EmptyHint(icon: "questionmark.folder", title: "Lecture not found", message: "It may have been deleted on another device.")
        }
    }

    @ViewBuilder
    private func content(_ lecture: LectureDoc) -> some View {
        switch section {
        case .transcript: TranscriptView(lecture: lecture)
        case .notes: NotesView(lecture: lecture)
        case .chat: ChatView(scope: .lecture(lectureID))
        case .quiz: QuizView(scope: .lecture(lectureID))
        case .cards: CardsView(scope: .lecture(lectureID))
        }
    }

    private func exportText(_ l: LectureDoc) -> String {
        var out = "# \(l.title)\n"
        if !l.course.isEmpty { out += "*\(l.course)*\n" }
        if !l.notes.isEmpty { out += "\n## Notes\n\n\(l.notes)\n" }
        out += "\n## Transcript\n\n" + l.transcript.map { "[\(formatTimestamp($0.t))] \($0.text)" }.joined(separator: "\n")
        return out
    }
}

// MARK: Transcript

struct TranscriptLineView: View {
    var line: TranscriptLine
    var onTap: (() -> Void)?

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Button { onTap?() } label: {
                Text(formatTimestamp(line.t)).font(.caption.monospacedDigit())
                    .foregroundStyle(onTap == nil ? Theme.faint : Theme.accent)
            }
            .buttonStyle(.plain)
            .disabled(onTap == nil)
            .frame(width: 48, alignment: .leading)
            Text(line.text)
                .font(.system(.body, design: .serif))
                .foregroundStyle(line.sideTalk ? Theme.faint : Theme.ink)
                .strikethrough(line.sideTalk, color: Theme.faint.opacity(0.6))
            if line.sideTalk {
                Spacer(minLength: 0)
                Text("Side talk").font(.caption2.weight(.semibold)).foregroundStyle(Theme.warn)
                    .padding(.horizontal, 7).padding(.vertical, 2).background(Theme.warnSoft, in: .capsule)
            }
        }
    }
}

struct TranscriptView: View {
    @Environment(AppModel.self) private var model
    var lecture: LectureDoc
    @State private var hideSideTalk = false
    @State private var find = ""

    var body: some View {
        let sideCount = lecture.transcript.filter(\.sideTalk).count
        let canPlay = model.store.hasAudio(lecture.id)
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                header
                if lecture.transcript.isEmpty {
                    EmptyHint(icon: "text.alignleft", title: "No transcript",
                              message: model.recorder.lectureID == lecture.id ? "Recording. Lines appear as you go." : "This lecture has no transcript.")
                } else {
                    HStack {
                        if sideCount > 0 {
                            Toggle("Hide side talk (\(sideCount))", isOn: $hideSideTalk).toggleStyle(.button).font(.footnote)
                        } else if model.busy.contains("sidetalk:\(lecture.id)") {
                            Label("Checking for side talk…", systemImage: "person.2").font(.footnote).foregroundStyle(Theme.muted)
                        } else if !lecture.sideTalkChecked && model.settings.aiReady {
                            Button("Find side talk", systemImage: "person.2") { Task { await model.detectSideTalk(lecture.id) } }
                                .font(.footnote).buttonStyle(.glass)
                        }
                        Spacer()
                    }
                    Sheet {
                        LazyVStack(alignment: .leading, spacing: 12) {
                            ForEach(Array(lecture.transcript.enumerated()), id: \.offset) { i, line in
                                if !(hideSideTalk && line.sideTalk),
                                   find.isEmpty || line.text.localizedCaseInsensitiveContains(find) {
                                    TranscriptLineView(line: line, onTap: canPlay ? {
                                        model.player.play(model.store.audioURL(for: lecture.id), lectureID: lecture.id, at: line.t)
                                    } : nil)
                                    .contextMenu {
                                        Button(line.sideTalk ? "This is part of the lecture" : "Mark as side talk",
                                               systemImage: line.sideTalk ? "checkmark.circle" : "person.2") {
                                            model.toggleSideTalk(lecture.id, line: i)
                                        }
                                        Button("Copy", systemImage: "doc.on.doc") { UIPasteboard.general.string = line.text }
                                    }
                                }
                            }
                        }
                    }
                    Text("Touch and hold a line to mark it as side talk. Side talk is left out of notes, chat and quizzes.")
                        .font(.caption).foregroundStyle(Theme.faint)
                }
            }
            .padding(16)
        }
        .searchable(text: $find, placement: .toolbar, prompt: "Find in transcript")
        .overlay(alignment: .bottom) { PlayerBar(lectureID: lecture.id) }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            if !lecture.course.isEmpty {
                let id = model.store.course(named: lecture.course)?.id
                HStack(spacing: 6) { CourseChip(id: id); Text(lecture.course) }
                    .font(.subheadline.weight(.semibold)).foregroundStyle(Theme.courseColor(id))
            }
            Text(lecture.title).font(.title2.weight(.bold))
            HStack(spacing: 12) {
                Label(Date(timeIntervalSince1970: lecture.createdAt).formatted(date: .abbreviated, time: .shortened), systemImage: "calendar")
                if lecture.duration > 0 { Label(formatDuration(lecture.duration), systemImage: "clock") }
            }
            .font(.footnote).foregroundStyle(Theme.muted)
        }
    }
}

struct PlayerBar: View {
    @Environment(AppModel.self) private var model
    var lectureID: String
    var body: some View {
        if model.player.lectureID == lectureID {
            HStack(spacing: 14) {
                Button { model.player.toggle() } label: { Image(systemName: model.player.isPlaying ? "pause.fill" : "play.fill") }
                Text(formatTimestamp(model.player.position)).monospacedDigit()
                Button { model.player.stop() } label: { Image(systemName: "xmark") }
            }
            .font(.headline)
            .padding(.horizontal, 18).padding(.vertical, 12)
            .glassEffect(.regular.interactive(), in: .capsule)
            .padding(.bottom, 12)
        }
    }
}

// MARK: Notes

struct NotesView: View {
    @Environment(AppModel.self) private var model
    var lecture: LectureDoc
    @State private var editing = false
    @State private var draft = ""

    var body: some View {
        let live = model.streaming["notes:\(lecture.id)"]
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if editing {
                    TextEditor(text: $draft).font(.system(.body, design: .monospaced)).frame(minHeight: 420)
                        .scrollContentBackground(.hidden).padding(8)
                        .background(Theme.surface, in: .rect(cornerRadius: 16))
                } else if let live {
                    Sheet { if live.isEmpty { ThinkingLabel(text: "Reading the lecture") } else { MarkdownText(text: live) } }
                } else if lecture.notes.isEmpty {
                    EmptyHint(icon: "doc.text", title: "No notes yet", message: model.settings.aiReady
                              ? "Write study notes from the transcript." : "Add your Anthropic API key in Settings to write notes.")
                    if model.settings.aiReady && !lecture.transcript.isEmpty {
                        Button("Write notes", systemImage: "sparkles.rectangle.stack") { Task { await model.generateNotes(lecture.id) } }
                            .buttonStyle(.glassProminent).frame(maxWidth: .infinity)
                    }
                } else {
                    Sheet {
                        MarkdownText(text: lecture.notes, onTimestamp: model.store.hasAudio(lecture.id) ? { t in
                            model.player.play(model.store.audioURL(for: lecture.id), lectureID: lecture.id, at: t)
                        } : nil)
                    }
                    if !lecture.deepDives.isEmpty {
                        Text("Deep dives").font(.footnote.weight(.semibold)).foregroundStyle(Theme.faint).textCase(.uppercase)
                        ForEach(lecture.deepDives, id: \.createdAt) { dive in
                            DisclosureGroup(dive.topic) { MarkdownText(text: dive.content, size: 15).padding(.top, 8) }
                                .padding(14).background(Theme.surface, in: .rect(cornerRadius: 14))
                        }
                    }
                }
            }
            .padding(16)
        }
        .overlay(alignment: .bottom) { PlayerBar(lectureID: lecture.id) }
        .toolbar {
            ToolbarItemGroup(placement: .bottomBar) {
                if editing {
                    Button("Cancel") { editing = false }
                    Spacer()
                    Button("Save") {
                        model.store.update(lecture.id) { $0.notes = draft }
                        editing = false
                        model.syncSoon()
                    }
                    .buttonStyle(.glassProminent)
                } else if live == nil && !lecture.notes.isEmpty {
                    Button("Rewrite", systemImage: "arrow.clockwise") { Task { await model.generateNotes(lecture.id) } }
                        .disabled(!model.settings.aiReady)
                    Spacer()
                    Button("Edit", systemImage: "pencil") { draft = lecture.notes; editing = true }
                    ShareLink(item: lecture.notes)
                }
            }
        }
    }
}

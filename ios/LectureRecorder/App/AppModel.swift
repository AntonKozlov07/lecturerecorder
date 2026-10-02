import Foundation
import Observation
import SwiftUI

/// Ties everything together: the library on disk, settings, recording, AI features and sync.
@MainActor
@Observable
final class AppModel {
    let store: LibraryStore
    let settings: AppSettings
    let recorder = Recorder()
    let player = Player()

    // Work in progress, keyed by lecture or course id.
    var streaming: [String: String] = [:]          // "notes:<id>" / "chat:<id>" -> text so far
    var busy: Set<String> = []                      // "quiz:<id>", "cards:<id>", "grade:<quiz>", "sidetalk:<id>"
    var processing: Set<String> = []                // lectures being finished after recording
    var errorMessage: String?

    // Sync status
    var syncing = false
    var lastSync: Date?
    var syncError: String?
    var lastSyncResult: GitHubSync.Stats?
    private var syncedRevision = -1
    private var syncLoop: Task<Void, Never>?

    init(store: LibraryStore? = nil, settings: AppSettings? = nil) {
        self.store = store ?? LibraryStore(root: LibraryStore.defaultRoot())
        self.settings = settings ?? AppSettings()
        Theme.library = self.store
    }

    var ai: StudyAI { StudyAI(client: ClaudeClient(apiKey: settings.apiKey, model: settings.model)) }

    func fail(_ error: Error) { errorMessage = error.localizedDescription }

    // MARK: Recording

    func startRecording(title: String, course: String) async {
        let lecture = LectureDoc(id: newID(), title: title.isEmpty ? "Untitled lecture" : title,
                                 course: course.trimmingCharacters(in: .whitespaces), createdAt: now())
        do {
            try await recorder.start(lectureID: lecture.id, audioURL: store.audioURL(for: lecture.id),
                                     language: settings.language)
            processing.insert(lecture.id)
            store.save(lecture)
        } catch {
            fail(error)
        }
    }

    /// Stop recording, save the transcript, then flag side talk and write notes in the background.
    func stopRecording() async {
        guard let id = recorder.lectureID else { return }
        let (lines, duration) = await recorder.stop()
        store.update(id) { $0.transcript = lines; $0.duration = duration }
        Task { await finishLecture(id) }
    }

    func finishLecture(_ id: String) async {
        defer { processing.remove(id); syncSoon() }
        guard settings.aiReady, let lecture = store.lecture(id), !lecture.transcript.isEmpty else { return }
        if settings.filterSideTalk { await detectSideTalk(id) }
        if settings.autoNotes, store.lecture(id)?.notes.isEmpty == true { await generateNotes(id) }
    }

    // MARK: Lecture AI features

    func generateNotes(_ id: String) async {
        guard let lecture = store.lecture(id) else { return }
        let key = "notes:\(id)"
        streaming[key] = ""
        defer { streaming[key] = nil }
        do {
            var text = ""
            for try await chunk in ai.notes(try StudyAI.lectureContext(lecture)) {
                text += chunk
                streaming[key] = text
            }
            store.update(id) { $0.notes = text }
        } catch {
            fail(error)
        }
    }

    func detectSideTalk(_ id: String) async {
        guard let lecture = store.lecture(id), !lecture.transcript.isEmpty else { return }
        busy.insert("sidetalk:\(id)")
        defer { busy.remove("sidetalk:\(id)") }
        var flagged = Set<Int>()
        let lines = lecture.transcript.map { "[\(formatTimestamp($0.t))] \($0.text)" }
        do {
            var start = 0
            while start < lines.count {
                let end = min(lines.count, start + 150)
                let hits = try await ai.detectSideTalk(title: lecture.title, lines: Array(lines[start..<end]),
                                                       context: Array(lines[max(0, start - 6)..<start]))
                flagged.formUnion(hits.map { $0 + start })
                start = end
            }
            store.update(id) { doc in
                for i in doc.transcript.indices { doc.transcript[i].sideTalk = flagged.contains(i) }
                doc.sideTalkChecked = true
            }
        } catch {
            fail(error)
        }
    }

    func toggleSideTalk(_ id: String, line: Int) {
        store.update(id) { $0.transcript[line].sideTalk.toggle() }
    }

    // MARK: Study features for a lecture or a course

    enum Scope: Hashable {
        case lecture(String), course(String)
        var id: String { switch self { case .lecture(let i), .course(let i): return i } }
    }

    func context(_ scope: Scope) throws -> StudyAI.Context {
        switch scope {
        case .lecture(let id):
            guard let l = store.lecture(id) else { throw StudyAI.StudyError.noMaterial("Lecture not found.") }
            return try StudyAI.lectureContext(l)
        case .course(let id):
            guard let c = store.courses[id] else { throw StudyAI.StudyError.noMaterial("Course not found.") }
            return try StudyAI.courseContext(c, store: store)
        }
    }

    func chatHistory(_ scope: Scope) -> [ChatMessage] {
        switch scope {
        case .lecture(let id): return store.lecture(id)?.chat ?? []
        case .course(let id): return store.courses[id]?.chat ?? []
        }
    }

    private func edit(_ scope: Scope, lecture: (inout LectureDoc) -> Void, course: (inout CourseDoc) -> Void) {
        switch scope {
        case .lecture(let id): store.update(id, lecture)
        case .course(let id): store.update(course: id, course)
        }
    }

    func sendChat(_ scope: Scope, question: String) async {
        let q = question.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !q.isEmpty else { return }
        let key = "chat:\(scope.id)"
        streaming[key] = ""
        pendingQuestion[scope.id] = q
        defer { streaming[key] = nil; pendingQuestion[scope.id] = nil }
        do {
            var text = ""
            for try await chunk in ai.chat(try context(scope), history: chatHistory(scope), question: q) {
                text += chunk
                streaming[key] = text
            }
            let asked = ChatMessage(role: "user", content: q, createdAt: now())
            let answer = ChatMessage(role: "assistant", content: text, createdAt: now() + 0.001)
            edit(scope, lecture: { $0.chat += [asked, answer] }, course: { $0.chat += [asked, answer] })
            syncSoon()
        } catch {
            fail(error)
        }
    }

    var pendingQuestion: [String: String] = [:]

    func clearChat(_ scope: Scope) {
        edit(scope, lecture: { $0.chat = [] }, course: { $0.chat = [] })
    }

    func quizzes(_ scope: Scope) -> [Quiz] {
        switch scope {
        case .lecture(let id): return (store.lecture(id)?.quizzes ?? []).sorted { $0.createdAt > $1.createdAt }
        case .course(let id): return (store.courses[id]?.quizzes ?? []).sorted { $0.createdAt > $1.createdAt }
        }
    }

    func makeQuiz(_ scope: Scope, count: Int, difficulty: String, focus: String, kind: String) async -> Quiz? {
        busy.insert("quiz:\(scope.id)")
        defer { busy.remove("quiz:\(scope.id)") }
        do {
            let quiz = try await ai.quiz(try context(scope), count: count, difficulty: difficulty, focus: focus, kind: kind)
            edit(scope, lecture: { $0.quizzes.append(quiz) }, course: { $0.quizzes.append(quiz) })
            return quiz
        } catch {
            fail(error)
            return nil
        }
    }

    func submitQuiz(_ scope: Scope, quiz: Quiz, answers: [Answer]) async {
        var done = quiz
        done.answers = answers
        if quiz.kind == "written" {
            busy.insert("grade:\(quiz.createdAt)")
            defer { busy.remove("grade:\(quiz.createdAt)") }
            do {
                let texts = answers.map { if case .text(let s) = $0 { return s } else { return "" } }
                let grades = try await ai.grade(try context(scope), quiz: quiz, answers: texts)
                done.grading = grades
                done.score = grades.reduce(0) { $0 + ($1.verdict == "correct" ? 1 : $1.verdict == "partial" ? 0.5 : 0) }
            } catch {
                fail(error)
                return
            }
        } else {
            done.score = Double(zip(quiz.questions, answers).filter { q, a in
                if case .choice(let i) = a { return i == q.answerIndex } else { return false }
            }.count)
        }
        let replace: ([Quiz]) -> [Quiz] = { list in list.map { $0.createdAt == quiz.createdAt ? done : $0 } }
        edit(scope, lecture: { $0.quizzes = replace($0.quizzes) }, course: { $0.quizzes = replace($0.quizzes) })
        syncSoon()
    }

    func deleteQuiz(_ scope: Scope, quiz: Quiz) {
        edit(scope, lecture: { $0.quizzes.removeAll { $0.createdAt == quiz.createdAt } },
             course: { $0.quizzes.removeAll { $0.createdAt == quiz.createdAt } })
    }

    func flashcards(_ scope: Scope) -> [Flashcard] {
        switch scope {
        case .lecture(let id): return store.lecture(id)?.flashcards ?? []
        case .course(let id): return store.courses[id]?.flashcards ?? []
        }
    }

    func makeFlashcards(_ scope: Scope, count: Int) async {
        busy.insert("cards:\(scope.id)")
        defer { busy.remove("cards:\(scope.id)") }
        do {
            let cards = try await ai.flashcards(try context(scope), count: count)
            edit(scope, lecture: { $0.flashcards = cards }, course: { $0.flashcards = cards })
            syncSoon()
        } catch {
            fail(error)
        }
    }

    // MARK: Sync

    func startSyncLoop() {
        syncLoop?.cancel()
        syncLoop = Task { [weak self] in
            while !Task.isCancelled {
                await self?.syncIfNeeded()
                try? await Task.sleep(for: .seconds(120))
            }
        }
    }

    func syncSoon() {
        Task { [weak self] in
            try? await Task.sleep(for: .seconds(2))
            await self?.sync()
        }
    }

    func syncIfNeeded() async {
        await sync()
    }

    func sync() async {
        guard settings.syncConfigured, !syncing else { return }
        syncing = true
        defer { syncing = false }
        let engine = GitHubSync(store: store, repo: AppSettings.normalizeRepo(settings.githubRepo), token: settings.githubToken)
        engine.busyLectures = processing.union(recorder.lectureID.map { [$0] } ?? [])
        do {
            lastSyncResult = try await engine.run()
            lastSync = Date()
            syncError = nil
        } catch {
            syncError = error.localizedDescription
        }
    }

    // MARK: Sample data for UI tests and screenshots

    func loadSampleData() {
        store.reset()
        let course = CourseDoc(id: "c0ffee000001", name: "PHYS 201", createdAt: now() - 86400 * 3,
                               materials: [Material(id: "f11e00000001", filename: "Chapter 4 - Entropy.pdf", kind: "pdf",
                                                    pages: 18, size: 1_200_000, createdAt: now() - 86400 * 3,
                                                    text: "[Page 1]\nEntropy and the second law.", file: nil)])
        store.save(course)
        var lecture = LectureDoc(id: "1ec7000000a1", title: "Week 3: Entropy", course: "PHYS 201",
                                 createdAt: now() - 86400, duration: 2875)
        lecture.transcript = [
            TranscriptLine(t: 0, end: 6, text: "Okay, let's get started. Today is all about entropy."),
            TranscriptLine(t: 6, end: 14, text: "Entropy counts how many microstates are consistent with a macrostate."),
            TranscriptLine(t: 14, end: 18, text: "Dude, are we still getting pizza after this?", sideTalk: true),
            TranscriptLine(t: 18, end: 27, text: "Boltzmann wrote it as S equals k log W, and that's on his tombstone."),
            TranscriptLine(t: 27, end: 36, text: "The second law says the entropy of an isolated system never decreases."),
        ]
        lecture.sideTalkChecked = true
        lecture.notes = """
        # Entropy and the Second Law

        Entropy measures how many microscopic arrangements fit what we observe. [00:06]

        ## Key concepts
        ### Microstates and macrostates
        A **macrostate** is what we measure; a **microstate** is one exact arrangement of particles.

        ### Boltzmann's formula
        $S = k_B \\ln W$, where $W$ is the number of microstates. [00:18]

        ## Things the lecturer emphasized
        - The second law applies to **isolated** systems. [00:27]
        """
        lecture.chat = [ChatMessage(role: "user", content: "Why does entropy only increase?", createdAt: now() - 3600),
                        ChatMessage(role: "assistant", content: "Because there are vastly more disordered microstates than ordered ones, so a system wandering randomly almost always ends up in a higher-entropy macrostate. [00:27]", createdAt: now() - 3599)]
        lecture.quizzes = [Quiz(createdAt: now() - 1800, kind: "choice", difficulty: "medium", questions: [
            QuizQuestion(question: "What does entropy count?", options: ["Energy", "Microstates", "Pressure", "Particles"],
                         answerIndex: 1, explanation: "Entropy counts microstates consistent with a macrostate.", source: "00:06"),
            QuizQuestion(question: "The second law applies to which systems?", options: ["Open", "Closed", "Isolated", "All"],
                         answerIndex: 2, explanation: "Only isolated systems are guaranteed non-decreasing entropy.", source: "00:27"),
        ])]
        lecture.flashcards = [Flashcard(front: "Boltzmann's entropy formula", back: "$S = k_B \\ln W$", source: "00:18"),
                              Flashcard(front: "What is a microstate?", back: "One exact arrangement of the particles.", source: "00:06")]
        store.save(lecture)
        store.save(LectureDoc(id: "1ec7000000a2", title: "Week 2: Heat engines", course: "PHYS 201",
                              createdAt: now() - 86400 * 8, duration: 3010,
                              transcript: [TranscriptLine(t: 0, end: 5, text: "Heat engines turn heat into work.")]))
        store.save(LectureDoc(id: "1ec7000000b1", title: "Cell membranes", course: "BIO 110", createdAt: now() - 86400 * 2,
                              duration: 2400, transcript: [TranscriptLine(t: 0, end: 5, text: "Membranes are lipid bilayers.")]))
    }
}

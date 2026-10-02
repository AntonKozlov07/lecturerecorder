import XCTest
@testable import LectureRecorder

@MainActor
final class SyncTests: XCTestCase {
    /// A lecture exactly as the PC app writes it (lecturerecorder/sync.py export_lecture).
    static let pcLecture = """
    {
     "chat": [{"content": "What is entropy?", "created_at": 1790000200.5, "role": "user"},
              {"content": "A count of microstates.", "created_at": 1790000201.25, "role": "assistant"}],
     "course": "PHYS 201", "created_at": 1790000000.0, "deep_dives": [], "duration": 30.0,
     "flashcards": [{"back": "S = k ln W", "front": "Boltzmann", "source": "00:04"}],
     "format": 1, "id": "3f9a0c1d2e4b", "modified_at": 1790003600.0, "notes": "# Notes",
     "quizzes": [{"answers": null, "created_at": 1790000300.0, "difficulty": "medium", "grading": null, "kind": "choice",
                  "questions": [{"answer_index": 1, "explanation": "e", "options": ["a", "b", "c", "d"],
                                 "question": "Q?", "source": "00:04"}], "score": null},
                 {"answers": [1, null], "created_at": 1790000400.0, "difficulty": "hard", "grading": null, "kind": "choice",
                  "questions": [], "score": 1.0}],
     "side_talk_checked": true, "title": "Week 1",
     "topics": [{"summary": "s", "timestamp": "00:04", "title": "t"}],
     "transcript": [{"end": 4.0, "side_talk": false, "t": 0.0, "text": "Entropy counts microstates."},
                    {"end": 8.0, "side_talk": true, "t": 4.0, "text": "lol pizza later?"}]
    }
    """

    func testReadsPCDocuments() throws {
        let doc = try DocCoding.decoder.decode(LectureDoc.self, from: Data(Self.pcLecture.utf8))
        XCTAssertEqual(doc.title, "Week 1")
        XCTAssertEqual(doc.transcript.count, 2)
        XCTAssertTrue(doc.transcript[1].sideTalk)
        XCTAssertEqual(doc.quizzes[0].questions[0].answerIndex, 1)
        XCTAssertNil(doc.quizzes[0].answers)
        XCTAssertEqual(doc.quizzes[1].answers, [.choice(1), Answer.none])
        XCTAssertFalse(doc.aiTranscript.contains("pizza"), "side talk is left out of what the AI reads")

        // Written back, the keys are the snake_case ones the PC expects.
        let json = String(decoding: try DocCoding.encoder.encode(doc), as: UTF8.self)
        for key in ["\"side_talk\"", "\"created_at\"", "\"answer_index\"", "\"side_talk_checked\"", "\"deep_dives\""] {
            XCTAssertTrue(json.contains(key), "missing \(key)")
        }
    }

    func testMergeCombinesChatAndPrefersAnsweredQuiz() throws {
        let base = try DocCoding.decoder.decode(LectureDoc.self, from: Data(Self.pcLecture.utf8))
        var phone = base
        phone.modifiedAt = 1790009000
        phone.chat.append(ChatMessage(role: "user", content: "From the phone", createdAt: 1790008000))
        phone.quizzes[0].answers = [.choice(1)]
        phone.quizzes[0].score = 1
        var pc = base
        pc.modifiedAt = 1790009500
        pc.title = "Week 1: Entropy"
        pc.chat.append(ChatMessage(role: "user", content: "From the PC", createdAt: 1790008500))

        let merged = GitHubSync.merge(phone, pc)
        XCTAssertEqual(merged.title, "Week 1: Entropy", "newer document wins for single values")
        XCTAssertEqual(merged.chat.map(\.content).suffix(2), ["From the phone", "From the PC"])
        XCTAssertEqual(merged.quizzes.first?.answers, [.choice(1)], "an answered quiz beats an unanswered copy")
    }

    func testCourseColorsMatchThePCApp() {
        // Values computed with the PC app's hashId() and assignCourseColors() in app.js.
        XCTAssertEqual(Theme.courseColorIndex("c0ffee000001"), 6)
        XCTAssertEqual(Theme.courseColorIndex("a81c3e0f9b27"), 0)
        XCTAssertEqual(Theme.courseColorIndex("3f9a0c1d2e4b"), 6)
        let courses = [("c0ffee000001", 1.0), ("a81c3e0f9b27", 2.0), ("3f9a0c1d2e4b", 3.0)]
            .map { CourseDoc(id: $0.0, name: $0.0, createdAt: $0.1) }
        // The newest course would clash with the oldest, so it moves to the next free color.
        XCTAssertEqual(Theme.assignColors(courses), ["c0ffee000001": 6, "a81c3e0f9b27": 0, "3f9a0c1d2e4b": 7])
    }

    func testGitBlobShaMatchesGit() {
        // `printf 'hello\n' | git hash-object --stdin`
        XCTAssertEqual(GitHubSync.gitBlobSha(Data("hello\n".utf8)), "ce013625030ba8dba906f756967f9e9ca394464a")
    }

    func testRepoNameNormalising() {
        XCTAssertEqual(AppSettings.normalizeRepo("https://github.com/me/lecture-sync.git"), "me/lecture-sync")
        XCTAssertEqual(AppSettings.normalizeRepo(" me/lecture-sync/ "), "me/lecture-sync")
    }

    /// Syncs with a fake GitHub that the PC app has already synced to (CI sets this up).
    func testSyncWithPCThroughFakeGitHub() async throws {
        guard let api = ProcessInfo.processInfo.environment["SYNC_TEST_API"].flatMap(URL.init(string:)) else {
            throw XCTSkip("SYNC_TEST_API not set; the cross-device test runs in CI")
        }
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("sync-test-\(UUID().uuidString)")
        let store = LibraryStore(root: root)
        let sync = GitHubSync(store: store, repo: "me/lectures", token: "test-token", api: api)

        let first = try await sync.run()
        XCTAssertGreaterThanOrEqual(first.downloaded, 2, "the PC's lecture and course arrive")
        let lecture = try XCTUnwrap(store.lectures.values.first { $0.title == "Week 1" })
        XCTAssertEqual(lecture.course, "PHYS 201")
        XCTAssertTrue(lecture.transcript.contains { $0.sideTalk })
        let course = try XCTUnwrap(store.course(named: "PHYS 201"))
        let material = try XCTUnwrap(course.materials.first)
        XCTAssertNotNil(store.materialURL(material), "the course file was downloaded")

        // Changes made on the phone reach GitHub (CI then checks they reach the PC).
        store.update(lecture.id) { $0.chat.append(ChatMessage(role: "user", content: "Asked on the iPhone", createdAt: now())) }
        store.save(LectureDoc(id: newID(), title: "Recorded on the iPhone", course: "PHYS 201", createdAt: now(), duration: 12,
                              transcript: [TranscriptLine(t: 0, end: 3, text: "Hello from the phone.")]))
        let second = try await sync.run()
        XCTAssertEqual(second.uploaded, 2)

        let third = try await sync.run()
        XCTAssertEqual(third, GitHubSync.Stats(), "nothing left to do")
    }
}

import Foundation

/// The study features, built on ClaudeClient. Prompts match the PC app (lecturerecorder/ai.py)
/// so both devices produce the same kind of notes, quizzes and cards.
struct StudyAI {
    var client: ClaudeClient

    static let baseInstructions = """
    You are a study assistant inside a lecture recording app. Lecture transcripts were produced automatically \
    by a speech recognizer, so they may contain misheard words, missing punctuation and no speaker labels. \
    Silently correct obvious transcription errors when the intended word is clear from context; if something \
    important is ambiguous, say so rather than guessing.

    Ground your answers in the student's material. When you add knowledge the material does not cover, make \
    that clear (for example "Beyond what was covered, ..."). Write in Markdown. Use headings, short paragraphs, \
    bullet lists and tables where they aid understanding. Use LaTeX between $...$ or $$...$$ only for real \
    math. Do not use emoji.
    """
    static let lectureScope = """
    The material is one lecture. Reference moments in it with [mm:ss] timestamps (square brackets, exactly \
    that format) when that helps the student find them.
    """
    static let courseScope = """
    The material is a whole course: several lecture transcripts and files the student uploaded (slides, \
    readings, handouts). Draw on all of it and connect ideas across lectures. When you point to where something \
    is covered, name the source in parentheses, for example (Lecture: Week 3 Entropy, 12:03) or (Chapter 4.pdf, page 12).
    """

    /// The material a request is about: content blocks for the first user turn.
    struct Context {
        var isCourse: Bool
        var blocks: [[String: Any]]

        var system: String { StudyAI.baseInstructions + "\n\n" + (isCourse ? StudyAI.courseScope : StudyAI.lectureScope) }

        func opening(_ task: String) -> [[String: Any]] {
            var b = blocks
            b[b.count - 1]["cache_control"] = ["type": "ephemeral"]
            return b + [["type": "text", "text": task]]
        }
    }

    enum StudyError: LocalizedError {
        case noMaterial(String)
        var errorDescription: String? { if case .noMaterial(let m) = self { return m }; return nil }
    }

    static func lectureContext(_ lecture: LectureDoc) throws -> Context {
        let transcript = lecture.aiTranscript
        guard !transcript.isEmpty else { throw StudyError.noMaterial("This lecture has no transcript yet.") }
        var header = "Lecture title: \(lecture.title)"
        if !lecture.course.isEmpty { header += "\nCourse: \(lecture.course)" }
        return Context(isCourse: false, blocks: [["type": "text", "text": "\(header)\n\n<transcript>\n\(transcript)\n</transcript>"]])
    }

    @MainActor
    static func courseContext(_ course: CourseDoc, store: LibraryStore) throws -> Context {
        let excluded = Set(course.excluded)
        var blocks: [[String: Any]] = [["type": "text", "text": "Course: \(course.name)"]]
        for lecture in store.lectures(in: course) where !excluded.contains("lecture:\(lecture.id)") {
            let t = lecture.aiTranscript
            if !t.isEmpty { blocks.append(["type": "text", "text": "<lecture title=\"\(lecture.title)\">\n\(t)\n</lecture>"]) }
        }
        for m in course.materials where !excluded.contains("material:\(m.id)") {
            let label = "<material name=\"\(m.filename)\">"
            if m.kind == "pdf_scan" || m.kind == "image" {
                guard let url = store.materialURL(m), let data = try? Data(contentsOf: url) else { continue }
                if m.kind == "pdf_scan" {
                    blocks.append(["type": "text", "text": "\(label) (scanned PDF follows)"])
                    blocks.append(["type": "document", "title": m.filename,
                                   "source": ["type": "base64", "media_type": "application/pdf", "data": data.base64EncodedString()]])
                } else {
                    blocks.append(["type": "text", "text": "\(label) (image follows)"])
                    blocks.append(["type": "image", "source": ["type": "base64", "media_type": imageType(m.filename),
                                                               "data": data.base64EncodedString()]])
                }
            } else if !m.text.isEmpty {
                blocks.append(["type": "text", "text": "\(label)\n\(m.text)\n</material>"])
            }
        }
        guard blocks.count > 1 else {
            throw StudyError.noMaterial("This course has no lecture transcripts or readable files selected yet.")
        }
        return Context(isCourse: true, blocks: blocks)
    }

    static func imageType(_ name: String) -> String {
        switch (name as NSString).pathExtension.lowercased() {
        case "png": return "image/png"
        case "webp": return "image/webp"
        case "gif": return "image/gif"
        default: return "image/jpeg"
        }
    }

    // MARK: Notes and chat

    static let notesPrompt = """
    Write study notes for this lecture that a student could revise from without re-listening. Structure:

    # <A clear title for the lecture>
    A 2-4 sentence overview of what the lecture covered and why it matters.

    ## Key concepts
    One subsection per major concept, in the order they were taught: explain it clearly, include definitions, \
    formulas, worked examples and the lecturer's own examples or analogies.

    ## Definitions
    A compact table of important terms.

    ## Things the lecturer emphasized
    Anything flagged as important, likely on an exam, a common mistake, or repeated. Include timestamps.

    ## Announcements and tasks
    Deadlines, readings, assignments or logistics mentioned. Omit this section if there were none.

    ## Open questions
    Points that were unclear in the recording or worth asking about.

    Be thorough but tight: no filler, no restating the same point twice.
    """

    func notes(_ ctx: Context) -> AsyncThrowingStream<String, Error> {
        client.stream(system: ctx.system, messages: [["role": "user", "content": ctx.opening(Self.notesPrompt)]], effort: "high")
    }

    static let chatPrefix = "(You are now chatting with the student about this material. Be direct and conversational; answer at the length the question deserves.)\n\n"

    func chat(_ ctx: Context, history: [ChatMessage], question: String) -> AsyncThrowingStream<String, Error> {
        var turns: [[String: Any]] = history.map { ["role": $0.role, "content": $0.content] }
        turns.append(["role": "user", "content": question])
        turns[0]["content"] = ctx.opening(Self.chatPrefix + (turns[0]["content"] as? String ?? ""))
        return client.stream(system: ctx.system, messages: turns, effort: "medium")
    }

    // MARK: Structured features

    private static func object(_ props: [String: Any]) -> [String: Any] {
        ["type": "object", "properties": props, "required": Array(props.keys), "additionalProperties": false]
    }

    private static func list(_ key: String, _ item: [String: Any]) -> [String: Any] {
        object([key: ["type": "array", "items": item]])
    }

    private func run(_ ctx: Context, _ task: String, _ schema: [String: Any], effort: String = "high") async throws -> [String: Any] {
        try await client.structured(system: ctx.system, messages: [["role": "user", "content": ctx.opening(task)]],
                                    schema: schema, effort: effort)
    }

    private static let difficulty = [
        "easy": "recall of key facts and definitions",
        "medium": "understanding and applying the concepts",
        "hard": "analysis, edge cases, multi-step reasoning and distinguishing closely related ideas",
    ]

    private func sourceHint(_ ctx: Context) -> String {
        ctx.isCourse ? "which lecture or file covers it, e.g. 'Lecture: Week 3, 12:03' or 'Chapter 4.pdf, page 12'"
                     : "the [mm:ss] timestamp where it was covered"
    }

    func quiz(_ ctx: Context, count: Int, difficulty: String, focus: String, kind: String) async throws -> Quiz {
        let level = Self.difficulty[difficulty] ?? Self.difficulty["medium"]!
        let focusLine = focus.trimmingCharacters(in: .whitespaces).isEmpty ? "" : "\nFocus on: \(focus)"
        let spread = ctx.isCourse ? " Spread questions across the different lectures and materials." : ""
        let str: [String: Any] = ["type": "string"]
        let data: [String: Any]
        if kind == "written" {
            data = try await run(ctx, """
            Write \(count) short-answer exam questions on this material testing \(level).\(focusLine)\(spread)

            Each should be answerable in 1-5 sentences (or a short calculation). Give a model answer, the key points \
            a full-marks answer must contain, and as source, \(sourceHint(ctx)).
            """, Self.list("questions", Self.object(["question": str, "model_answer": str,
                                                    "key_points": ["type": "array", "items": str], "source": str])))
        } else {
            data = try await run(ctx, """
            Write a \(count)-question multiple-choice quiz on this material testing \(level).\(focusLine)\(spread)

            Each question has exactly 4 options with one correct answer (answer_index is 0-3). Make distractors \
            plausible, vary the position of the correct answer, and do not use 'all of the above' or 'none of the \
            above'. The explanation should say why the answer is right and, briefly, why the tempting wrong option \
            is wrong. As source, give \(sourceHint(ctx)). Questions must be answerable from the material.
            """, Self.list("questions", Self.object(["question": str, "options": ["type": "array", "items": str],
                                                    "answer_index": ["type": "integer"], "explanation": str, "source": str])))
        }
        let raw = try JSONSerialization.data(withJSONObject: data["questions"] ?? [])
        var questions = try DocCoding.decoder.decode([QuizQuestion].self, from: raw)
        questions = questions.filter { q in
            kind == "written" ? !q.question.isEmpty
                : (q.options?.count ?? 0) >= 2 && (0..<(q.options?.count ?? 0)).contains(q.answerIndex ?? -1)
        }
        guard !questions.isEmpty else { throw StudyError.noMaterial("The quiz came back empty. Try again.") }
        return Quiz(createdAt: now(), kind: kind, difficulty: difficulty, questions: questions)
    }

    func grade(_ ctx: Context, quiz: Quiz, answers: [String]) async throws -> [Grade] {
        let items = zip(quiz.questions, answers).enumerated().map { i, pair in
            let (q, a) = pair
            let answer = a.trimmingCharacters(in: .whitespacesAndNewlines)
            return """
            <question n="\(i + 1)">
            \(q.question)
            <model_answer>\(q.modelAnswer ?? "")</model_answer>
            <key_points>\((q.keyPoints ?? []).joined(separator: "; "))</key_points>
            <student_answer>\(answer.isEmpty ? "(no answer)" : answer)</student_answer>
            </question>
            """
        }.joined(separator: "\n\n")
        let data = try await run(ctx, """
        Grade the student's answers to these questions, in order, one result per question. 'correct' means it \
        covers the key points (wording can differ); 'partial' means some key points are right but something \
        important is missing or wrong; 'incorrect' otherwise, including blank answers. Feedback is 1-3 sentences \
        addressed to the student: what they got right, what is missing or wrong, and the fix. Be fair and \
        encouraging, not lenient.

        \(items)
        """, Self.list("results", Self.object(["verdict": ["type": "string", "enum": ["correct", "partial", "incorrect"]],
                                               "feedback": ["type": "string"]])), effort: "medium")
        let raw = try JSONSerialization.data(withJSONObject: data["results"] ?? [])
        var grades = try DocCoding.decoder.decode([Grade].self, from: raw)
        while grades.count < quiz.questions.count { grades.append(Grade(verdict: "incorrect", feedback: "Could not be graded.")) }
        return Array(grades.prefix(quiz.questions.count))
    }

    func flashcards(_ ctx: Context, count: Int) async throws -> [Flashcard] {
        let str: [String: Any] = ["type": "string"]
        let spread = ctx.isCourse ? " Cover all the lectures and materials, not just one." : ""
        let data = try await run(ctx, """
        Create \(count) flashcards covering the most important material.\(spread) The front is a specific \
        question or term; the back is a concise answer (1-3 sentences, Markdown allowed). Prefer cards that test \
        understanding over trivia. Order them as the material was taught. As source, give \(sourceHint(ctx)).
        """, Self.list("cards", Self.object(["front": str, "back": str, "source": str])), effort: "medium")
        let raw = try JSONSerialization.data(withJSONObject: data["cards"] ?? [])
        return try DocCoding.decoder.decode([Flashcard].self, from: raw).filter { !$0.front.isEmpty && !$0.back.isEmpty }
    }

    // MARK: Side talk

    /// Indexes of lines that are side talk rather than lecture content. Uses Claude Haiku: small, cheap calls.
    func detectSideTalk(title: String, lines: [String], context: [String]) async throws -> Set<Int> {
        let ctx = context.isEmpty ? "" : "\nThe lines just before these, for context only:\n<earlier>\n\(context.joined(separator: "\n"))\n</earlier>"
        let numbered = lines.enumerated().map { "\($0 + 1). \($1)" }.joined(separator: "\n")
        let prompt = """
        These numbered lines come from an automatic transcript of a class recording. The student wants to separate \
        the lecture from side talk.

        Side talk: conversation that is not part of the class, such as students chatting or joking with each other, \
        personal conversations, phone calls, and remarks unrelated to the course.

        Not side talk: anything the instructor says about the subject (including jokes, stories and tangents the \
        instructor uses while teaching), questions and answers about the material, and course logistics such as \
        deadlines, readings and announcements. When unsure, treat a line as lecture content.

        Lecture: \(title)\(ctx)
        <lines>
        \(numbered)
        </lines>

        List the numbers of the side-talk lines. Return an empty list if there are none.
        """
        let data = try await client.structured(
            system: "", messages: [["role": "user", "content": prompt]],
            schema: Self.object(["side_talk": ["type": "array", "items": ["type": "integer"]]]),
            effort: "low", model: "claude-haiku-4-5")
        let numbers = data["side_talk"] as? [Int] ?? []
        return Set(numbers.filter { (1...lines.count).contains($0) }.map { $0 - 1 })
    }
}

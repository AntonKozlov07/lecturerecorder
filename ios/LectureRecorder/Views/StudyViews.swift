import SwiftUI

// Chat, quizzes and flashcards work the same for one lecture or a whole course.

struct ChatView: View {
    @Environment(AppModel.self) private var model
    var scope: AppModel.Scope
    @State private var draft = ""
    @FocusState private var focused: Bool

    private var suggestions: [String] {
        if case .course = scope {
            return ["What are the main themes of this course so far?", "Make me a one-page study guide for the exam.",
                    "What should I focus on if I only have two hours?"]
        }
        return ["Summarize this lecture in five bullet points.", "What did the lecturer say is likely to be on the exam?",
                "Explain the hardest idea in this lecture simply."]
    }

    var body: some View {
        let history = model.chatHistory(scope)
        let live = model.streaming["chat:\(scope.id)"]
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 14) {
                    if history.isEmpty && live == nil {
                        Text(model.settings.aiReady ? "Ask anything. Answers draw on the material and say when they go beyond it."
                                                    : "Add your Anthropic API key in Settings to chat.")
                            .font(.subheadline).foregroundStyle(Theme.muted)
                        ForEach(suggestions, id: \.self) { s in
                            Button { Task { await model.sendChat(scope, question: s) } } label: {
                                Text(s).frame(maxWidth: .infinity, alignment: .leading).padding(14)
                                    .background(Theme.surface, in: .rect(cornerRadius: 14))
                                    .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(Theme.border))
                            }
                            .buttonStyle(.plain)
                            .disabled(!model.settings.aiReady)
                        }
                    }
                    ForEach(history) { message in Bubble(message: message) }
                    if let live {
                        if let q = model.pendingQuestion[scope.id] { Bubble(message: ChatMessage(role: "user", content: q, createdAt: 0)) }
                        Sheet { if live.isEmpty { ThinkingLabel() } else { MarkdownText(text: live, size: 15) } }
                    }
                    Color.clear.frame(height: 1).id("end")
                }
                .padding(16)
            }
            .scrollDismissesKeyboard(.interactively)
            .onChange(of: live) { proxy.scrollTo("end", anchor: .bottom) }
            .onChange(of: history.count) { proxy.scrollTo("end", anchor: .bottom) }
            .onAppear { proxy.scrollTo("end", anchor: .bottom) }
        }
        .safeAreaInset(edge: .bottom) {
            HStack(alignment: .bottom, spacing: 10) {
                TextField("Ask a question", text: $draft, axis: .vertical)
                    .lineLimit(1...5)
                    .focused($focused)
                    .padding(.horizontal, 14).padding(.vertical, 10)
                    .glassEffect(.regular, in: .rect(cornerRadius: 20))
                    .accessibilityIdentifier("chat-input")
                Button {
                    let q = draft
                    draft = ""
                    Task { await model.sendChat(scope, question: q) }
                } label: {
                    Image(systemName: "arrow.up").font(.headline).padding(6)
                }
                .buttonStyle(.glassProminent)
                .disabled(draft.trimmingCharacters(in: .whitespaces).isEmpty || live != nil || !model.settings.aiReady)
            }
            .padding(.horizontal, 12).padding(.bottom, 8)
        }
        .toolbar {
            if !history.isEmpty && live == nil {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Clear", systemImage: "trash") { model.clearChat(scope) }
                }
            }
        }
    }
}

struct Bubble: View {
    var message: ChatMessage
    var body: some View {
        if message.role == "user" {
            HStack {
                Spacer(minLength: 50)
                Text(message.content)
                    .padding(.horizontal, 14).padding(.vertical, 10)
                    .foregroundStyle(Theme.paper)
                    .background(Theme.ink, in: .rect(cornerRadius: 18))
            }
        } else {
            Sheet { MarkdownText(text: message.content, size: 15) }
        }
    }
}

// MARK: Quiz

struct QuizView: View {
    @Environment(AppModel.self) private var model
    var scope: AppModel.Scope
    @State private var kind = "choice"
    @State private var count = 10
    @State private var difficulty = "medium"
    @State private var focus = ""
    @State private var openQuiz: Double?
    @State private var answers: [Answer] = []
    @State private var retake = false

    var body: some View {
        let quizzes = model.quizzes(scope)
        let quiz = quizzes.first { $0.createdAt == openQuiz } ?? (openQuiz == nil ? quizzes.first : nil)
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                setup
                if let quiz {
                    QuizBody(quiz: quiz, answers: $answers, graded: quiz.answers != nil && !retake,
                             grading: model.busy.contains("grade:\(quiz.createdAt)")) {
                        Task {
                            await model.submitQuiz(scope, quiz: quiz, answers: answers)
                            retake = false
                        }
                    } onRetake: {
                        answers = Array(repeating: .none, count: quiz.questions.count)
                        retake = true
                    }
                    .id(quiz.createdAt)
                    .onAppear { answers = quiz.answers ?? Array(repeating: .none, count: quiz.questions.count) }
                    .onChange(of: quiz.createdAt) { answers = quiz.answers ?? Array(repeating: .none, count: quiz.questions.count) }
                }
                if quizzes.count > 1 {
                    Text("Past quizzes").font(.footnote.weight(.semibold)).foregroundStyle(Theme.faint).textCase(.uppercase)
                    ForEach(quizzes) { q in
                        Button {
                            openQuiz = q.createdAt
                            retake = false
                        } label: {
                            HStack {
                                VStack(alignment: .leading) {
                                    Text(Date(timeIntervalSince1970: q.createdAt).formatted(date: .abbreviated, time: .shortened))
                                    Text("\(q.kind == "written" ? "Written" : "Multiple choice"), \(q.difficulty), \(q.questions.count) questions")
                                        .font(.caption).foregroundStyle(Theme.muted)
                                }
                                Spacer()
                                Text(q.score.map { "\(scoreText($0)) / \(q.questions.count)" } ?? "Not taken").foregroundStyle(Theme.muted)
                            }
                            .padding(12).background(Theme.surface, in: .rect(cornerRadius: 12))
                        }
                        .buttonStyle(.plain)
                        .contextMenu { Button("Delete", systemImage: "trash", role: .destructive) { model.deleteQuiz(scope, quiz: q) } }
                    }
                }
            }
            .padding(16)
        }
    }

    private var setup: some View {
        VStack(alignment: .leading, spacing: 10) {
            Picker("Type", selection: $kind) {
                Text("Multiple choice").tag("choice")
                Text("Written answers").tag("written")
            }
            .pickerStyle(.segmented)
            .onChange(of: kind) { count = kind == "written" ? 5 : 10 }
            HStack {
                Picker("Questions", selection: $count) { ForEach([5, 10, 15, 20], id: \.self) { Text("\($0) questions").tag($0) } }
                Picker("Difficulty", selection: $difficulty) {
                    Text("Easy").tag("easy"); Text("Medium").tag("medium"); Text("Hard").tag("hard")
                }
                Spacer()
            }
            TextField("Focus (optional)", text: $focus).textFieldStyle(.roundedBorder)
            let busy = model.busy.contains("quiz:\(scope.id)")
            Button {
                Task {
                    if let q = await model.makeQuiz(scope, count: count, difficulty: difficulty, focus: focus, kind: kind) {
                        openQuiz = q.createdAt
                        retake = false
                    }
                }
            } label: {
                Label(busy ? "Writing quiz…" : "New quiz", systemImage: "sparkles").frame(maxWidth: .infinity).padding(.vertical, 4)
            }
            .buttonStyle(.glassProminent)
            .disabled(busy || !model.settings.aiReady)
            NeedsKeyHint()
        }
        .padding(14)
        .background(Theme.surface, in: .rect(cornerRadius: 16))
    }
}

func scoreText(_ score: Double) -> String {
    score.rounded() == score ? String(Int(score)) : String(format: "%.1f", score)
}

struct QuizBody: View {
    var quiz: Quiz
    @Binding var answers: [Answer]
    var graded: Bool
    var grading: Bool
    var onSubmit: () -> Void
    var onRetake: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            if graded, let score = quiz.score {
                HStack(alignment: .firstTextBaseline) {
                    Text("\(scoreText(score)) / \(quiz.questions.count)").font(.largeTitle.weight(.bold))
                    Text(verdict(score / Double(quiz.questions.count))).foregroundStyle(Theme.muted)
                }
            } else {
                ProgressView(value: Double(answers.filter(\.isAnswered).count), total: Double(max(1, quiz.questions.count)))
                    .tint(Theme.accent)
            }
            ForEach(Array(quiz.questions.enumerated()), id: \.offset) { i, q in
                VStack(alignment: .leading, spacing: 8) {
                    Text(verbatim: "\(i + 1). \(LaTeX.render(in: q.question))").font(.headline)
                    if quiz.kind == "written" { written(i, q) } else { choices(i, q) }
                }
            }
            if graded {
                Button("Retake", systemImage: "arrow.counterclockwise", action: onRetake).buttonStyle(.glass)
            } else {
                Button(action: onSubmit) {
                    Label(grading ? "Grading…" : quiz.kind == "written" ? "Grade my answers" : "Check answers",
                          systemImage: "checkmark").frame(maxWidth: .infinity).padding(.vertical, 4)
                }
                .buttonStyle(.glassProminent)
                .disabled(grading)
                .accessibilityIdentifier("check-answers")
            }
        }
    }

    private func verdict(_ r: Double) -> String {
        r == 1 ? "Perfect." : r >= 0.8 ? "Strong." : r >= 0.6 ? "Getting there." : "Worth another pass."
    }

    @ViewBuilder
    private func choices(_ i: Int, _ q: QuizQuestion) -> some View {
        let options = q.options ?? []
        ForEach(Array(options.enumerated()), id: \.offset) { j, option in
            let chosen = i < answers.count && answers[i] == .choice(j)
            let correct = graded && j == q.answerIndex
            let wrong = graded && chosen && j != q.answerIndex
            Button {
                if !graded && i < answers.count { answers[i] = .choice(j) }
            } label: {
                HStack(alignment: .top, spacing: 10) {
                    Text(String("ABCDEFGH".dropFirst(j).prefix(1)))
                        .font(.caption.weight(.bold)).frame(width: 24, height: 24)
                        .foregroundStyle(chosen || correct || wrong ? Color.white : Theme.muted)
                        .background(correct ? Theme.ok : wrong ? Theme.danger : chosen ? Theme.ink : Color.clear, in: .rect(cornerRadius: 7))
                        .overlay(RoundedRectangle(cornerRadius: 7).strokeBorder(chosen || correct || wrong ? Color.clear : Theme.border))
                    Text(LaTeX.render(in: option)).foregroundStyle(Theme.ink).frame(maxWidth: .infinity, alignment: .leading)
                }
                .padding(12)
                .background(correct ? Theme.ok.opacity(0.12) : wrong ? Theme.danger.opacity(0.1) : Theme.surface, in: .rect(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(chosen && !graded ? Theme.ink : Theme.border))
            }
            .buttonStyle(.plain)
            .accessibilityLabel(option)
            .accessibilityIdentifier("option-\(i)-\(j)")
        }
        if graded, let e = q.explanation {
            MarkdownText(text: e + (q.source.map { "\n\n*Source: \($0)*" } ?? ""), size: 14).padding(.top, 2)
        }
    }

    @ViewBuilder
    private func written(_ i: Int, _ q: QuizQuestion) -> some View {
        if graded {
            let grade = quiz.grading?[safe: i]
            let text: String = { if case .text(let s) = answers[safe: i] ?? .none { return s } else { return "" } }()
            Text(text.isEmpty ? "No answer" : text).italic(text.isEmpty)
                .frame(maxWidth: .infinity, alignment: .leading).padding(12)
                .background(Theme.surface, in: .rect(cornerRadius: 12))
                .overlay(alignment: .leading) { Rectangle().fill(color(grade?.verdict)).frame(width: 3) }
            if let grade {
                Text(label(grade.verdict)).font(.caption.weight(.semibold)).foregroundStyle(color(grade.verdict))
                MarkdownText(text: grade.feedback, size: 14)
            }
            DisclosureGroup("Model answer") { MarkdownText(text: q.modelAnswer ?? "", size: 14).padding(.top, 6) }.font(.subheadline)
        } else {
            TextField("Your answer", text: Binding(
                get: { if case .text(let s) = answers[safe: i] ?? .none { return s } else { return "" } },
                set: { if i < answers.count { answers[i] = .text($0) } }), axis: .vertical)
                .lineLimit(3...8)
                .padding(12)
                .background(Theme.surface, in: .rect(cornerRadius: 12))
        }
    }

    private func color(_ verdict: String?) -> Color {
        verdict == "correct" ? Theme.ok : verdict == "partial" ? Theme.warn : Theme.danger
    }

    private func label(_ verdict: String) -> String {
        verdict == "correct" ? "Correct" : verdict == "partial" ? "Partly right" : "Not quite"
    }
}

extension Array {
    subscript(safe index: Int) -> Element? { indices.contains(index) ? self[index] : nil }
}

// MARK: Flashcards

struct CardsView: View {
    @Environment(AppModel.self) private var model
    var scope: AppModel.Scope
    @State private var order: [Int] = []
    @State private var position = 0
    @State private var flipped = false
    @State private var known = 0
    @State private var count = 20

    var body: some View {
        let cards = model.flashcards(scope)
        let busy = model.busy.contains("cards:\(scope.id)")
        ScrollView {
            VStack(spacing: 18) {
                if cards.isEmpty {
                    EmptyHint(icon: "rectangle.on.rectangle", title: "No flashcards yet",
                              message: model.settings.aiReady ? "Make a deck from this material." : "Add your Anthropic API key in Settings.")
                } else if position < order.count, let card = cards[safe: order[position]] {
                    ProgressView(value: Double(position), total: Double(order.count)).tint(Theme.accent)
                    CardFace(card: card, flipped: flipped)
                        .onTapGesture { withAnimation(.spring(duration: 0.45)) { flipped.toggle() } }
                    Text("\(position + 1) of \(order.count)").font(.footnote.monospacedDigit()).foregroundStyle(Theme.muted)
                    GlassEffectContainer(spacing: 12) {
                        HStack(spacing: 12) {
                            if flipped {
                                Button("Again") { next(again: true) }.buttonStyle(.glass).frame(maxWidth: .infinity)
                                Button("Got it") { next(again: false) }.buttonStyle(.glassProminent).frame(maxWidth: .infinity)
                            } else {
                                Button("Show answer") { withAnimation(.spring(duration: 0.45)) { flipped = true } }
                                    .buttonStyle(.glassProminent).frame(maxWidth: .infinity)
                            }
                        }
                        .font(.headline)
                    }
                } else {
                    EmptyHint(icon: "checkmark.seal", title: "Deck finished",
                              message: "\(known) of \(order.count) marked as known.")
                    Button("Go again", systemImage: "arrow.counterclockwise") { restart(cards.count) }.buttonStyle(.glass)
                }
                if !cards.isEmpty && position < order.count {
                    Button("Shuffle", systemImage: "shuffle") { order.shuffle(); position = 0; flipped = false }
                        .font(.subheadline)
                }
                VStack(spacing: 8) {
                    HStack(spacing: 10) {
                        Button {
                            Task { await model.makeFlashcards(scope, count: count); restart(model.flashcards(scope).count) }
                        } label: {
                            Label(busy ? "Making cards…" : cards.isEmpty ? "Make flashcards" : "Make a new deck", systemImage: "sparkles")
                                .frame(maxWidth: .infinity).padding(.vertical, 4)
                        }
                        .buttonStyle(.glass)
                        .disabled(busy || !model.settings.aiReady)
                        Menu {
                            Picker("Deck size", selection: $count) { ForEach([10, 20, 30, 40], id: \.self) { Text("\($0) cards").tag($0) } }
                        } label: {
                            Text("\(count) cards").monospacedDigit().padding(.vertical, 4)
                        }
                        .buttonStyle(.glass)
                        .accessibilityLabel("Cards in the new deck: \(count)")
                    }
                    NeedsKeyHint()
                }
                .padding(.top, 8)
            }
            .padding(16)
        }
        .onAppear { if order.count != cards.count { restart(cards.count) } }
    }

    private func restart(_ n: Int) {
        order = Array(0..<n)
        position = 0
        known = 0
        flipped = false
    }

    private func next(again: Bool) {
        if again { order.append(order[position]) } else { known += 1 }
        position += 1
        flipped = false
    }
}

struct CardFace: View {
    var card: Flashcard
    var flipped: Bool

    var body: some View {
        ZStack {
            face(label: "Question") { Text(LaTeX.render(in: card.front)).font(.title3.weight(.semibold)).multilineTextAlignment(.center) }
                .opacity(flipped ? 0 : 1)
            face(label: "Answer") {
                VStack(spacing: 10) {
                    MarkdownText(text: card.back, size: 17)
                    if let s = card.source, !s.isEmpty { Text(s).font(.caption).foregroundStyle(Theme.faint) }
                }
            }
            .opacity(flipped ? 1 : 0)
            .rotation3DEffect(.degrees(180), axis: (x: 0, y: 1, z: 0))
        }
        .rotation3DEffect(.degrees(flipped ? 180 : 0), axis: (x: 0, y: 1, z: 0))
        .accessibilityIdentifier("card")
    }

    private func face<C: View>(label: String, @ViewBuilder content: () -> C) -> some View {
        VStack(spacing: 14) {
            Text(label.uppercased()).font(.caption2.weight(.semibold)).foregroundStyle(Theme.faint)
            Spacer(minLength: 0)
            content()
            Spacer(minLength: 0)
        }
        .padding(24)
        .frame(maxWidth: .infinity, minHeight: 300)
        .background(Theme.surface, in: .rect(cornerRadius: 24))
        .overlay(RoundedRectangle(cornerRadius: 24).strokeBorder(Theme.border))
        .shadow(color: .black.opacity(0.06), radius: 12, y: 6)
    }
}

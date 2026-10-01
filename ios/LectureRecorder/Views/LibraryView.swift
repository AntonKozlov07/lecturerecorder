import SwiftUI

struct LibraryView: View {
    @Environment(AppModel.self) private var model
    @State private var search = ""
    @State private var showNew = false
    @State private var showRecorder = false

    private var filtered: [LectureDoc] {
        let q = search.trimmingCharacters(in: .whitespaces).lowercased()
        let all = model.store.sortedLectures
        guard !q.isEmpty else { return all }
        return all.filter {
            $0.title.lowercased().contains(q) || $0.course.lowercased().contains(q) || $0.notes.lowercased().contains(q)
                || $0.transcript.contains { $0.text.lowercased().contains(q) }
        }
    }

    var body: some View {
        NavigationStack {
            ZStack(alignment: .bottomTrailing) {
                List {
                    if model.store.lectures.isEmpty {
                        EmptyHint(icon: "waveform", title: "No lectures yet",
                                  message: "Tap Record when a lecture starts. The transcript fills in as you go, and recording continues with the screen locked.")
                            .listRowBackground(Color.clear)
                    }
                    ForEach(groupedByDay(filtered), id: \.0) { day, lectures in
                        Section(day) {
                            ForEach(lectures) { lecture in
                                NavigationLink(value: lecture.id) { LectureRow(lecture: lecture) }
                                    .listRowBackground(Theme.surface)
                            }
                            .onDelete { offsets in
                                for i in offsets { model.store.deleteLecture(lectures[i].id) }
                                model.syncSoon()
                            }
                        }
                    }
                }
                .scrollContentBackground(.hidden)
                .paperBackground()
                .searchable(text: $search, prompt: "Search lectures and transcripts")
                .navigationTitle("Lectures")
                .navigationDestination(for: String.self) { LectureView(lectureID: $0) }
                .toolbar {
                    ToolbarItem(placement: .topBarTrailing) { SyncButton() }
                }
                .refreshable { await model.sync() }

                if !model.recorder.isActive {
                    Button {
                        showNew = true
                    } label: {
                        Label("Record", systemImage: "mic.fill")
                            .font(.headline)
                            .padding(.horizontal, 22).padding(.vertical, 14)
                    }
                    .buttonStyle(.glassProminent)
                    .tint(Theme.record)
                    .padding(.trailing, 20).padding(.bottom, 16)
                    .accessibilityIdentifier("record")
                }
            }
            .sheet(isPresented: $showNew) {
                NewRecordingSheet { title, course in
                    showNew = false
                    Task {
                        await model.startRecording(title: title, course: course)
                        if model.recorder.isActive { showRecorder = true }
                    }
                }
                .presentationDetents([.medium])
            }
            .fullScreenCover(isPresented: $showRecorder) { RecordingView() }
        }
    }

    private func groupedByDay(_ lectures: [LectureDoc]) -> [(String, [LectureDoc])] {
        let cal = Calendar.current
        var groups: [(String, [LectureDoc])] = []
        for l in lectures {
            let date = Date(timeIntervalSince1970: l.createdAt)
            let label = cal.isDateInToday(date) ? "Today" : cal.isDateInYesterday(date) ? "Yesterday"
                : cal.isDate(date, equalTo: .now, toGranularity: .weekOfYear) ? "This week"
                : date.formatted(.dateTime.month(.wide).year())
            if groups.last?.0 == label { groups[groups.count - 1].1.append(l) } else { groups.append((label, [l])) }
        }
        return groups
    }
}

struct LectureRow: View {
    @Environment(AppModel.self) private var model
    var lecture: LectureDoc

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            if !lecture.course.isEmpty {
                HStack(spacing: 6) {
                    CourseChip(id: model.store.course(named: lecture.course)?.id, size: 8)
                    Text(lecture.course).font(.caption.weight(.semibold))
                        .foregroundStyle(Theme.courseColor(model.store.course(named: lecture.course)?.id))
                }
            }
            Text(lecture.title).font(.body.weight(.semibold)).foregroundStyle(Theme.ink)
            HStack(spacing: 8) {
                Text(Date(timeIntervalSince1970: lecture.createdAt).formatted(date: .abbreviated, time: .shortened))
                if lecture.duration > 0 { Text(formatDuration(lecture.duration)) }
                if model.recorder.lectureID == lecture.id {
                    Label("Recording", systemImage: "record.circle").foregroundStyle(Theme.record)
                } else if model.processing.contains(lecture.id) {
                    Text("Writing notes").foregroundStyle(Theme.accent)
                }
            }
            .font(.caption).foregroundStyle(Theme.muted)
        }
        .padding(.vertical, 2)
    }
}

struct SyncButton: View {
    @Environment(AppModel.self) private var model
    var body: some View {
        Button {
            Task { await model.sync() }
        } label: {
            Image(systemName: model.syncError != nil ? "exclamationmark.icloud" : "arrow.triangle.2.circlepath")
                .symbolEffect(.rotate, isActive: model.syncing)
                .foregroundStyle(model.syncError != nil ? Theme.danger : Theme.ink)
        }
        .disabled(!model.settings.syncConfigured || model.syncing)
        .accessibilityLabel("Sync now")
    }
}

// MARK: Starting a recording

struct NewRecordingSheet: View {
    @Environment(AppModel.self) private var model
    var start: (String, String) -> Void
    @State private var title = "Lecture \(Date().formatted(.dateTime.month(.abbreviated).day()))"
    @State private var course = ""

    var body: some View {
        NavigationStack {
            Form {
                TextField("Title", text: $title).accessibilityIdentifier("title")
                Section("Course") {
                    TextField("Optional", text: $course)
                    if !model.store.courses.isEmpty {
                        ScrollView(.horizontal, showsIndicators: false) {
                            HStack {
                                ForEach(model.store.sortedCourses) { c in
                                    Button {
                                        course = c.name
                                    } label: {
                                        HStack(spacing: 6) { CourseChip(id: c.id); Text(c.name) }
                                    }
                                    .buttonStyle(.glass)
                                }
                            }
                        }
                    }
                }
                Section {
                    Label("Recording keeps going with the screen locked. The transcript is made on this iPhone.",
                          systemImage: "lock.iphone")
                        .font(.footnote).foregroundStyle(Theme.muted)
                }
            }
            .navigationTitle("New recording")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Start") { start(title, course) }
                        .buttonStyle(.glassProminent).tint(Theme.record)
                        .accessibilityIdentifier("start")
                }
            }
        }
    }
}

// MARK: The recording screen

struct RecordingView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @Namespace private var glass

    var body: some View {
        let r = model.recorder
        let lecture = r.lectureID.flatMap { model.store.lecture($0) }
        VStack(spacing: 0) {
            HStack {
                Button { dismiss() } label: { Image(systemName: "chevron.down").font(.headline) }
                    .buttonStyle(.glass)
                    .accessibilityLabel("Minimize")
                Spacer()
                if let course = lecture?.course, !course.isEmpty {
                    HStack(spacing: 6) {
                        CourseChip(id: model.store.course(named: course)?.id)
                        Text(course).font(.subheadline.weight(.semibold))
                    }
                    .padding(.horizontal, 12).padding(.vertical, 8)
                    .glassEffect()
                }
            }
            .padding(.horizontal, 20)

            VStack(spacing: 6) {
                Text(lecture?.title ?? "Recording").font(.title3.weight(.semibold)).multilineTextAlignment(.center)
                Text(formatTimestamp(r.elapsed))
                    .font(.system(size: 54, weight: .semibold, design: .rounded).monospacedDigit())
                    .contentTransition(.numericText())
                LevelMeter(levels: r.levels, active: r.phase == .recording).frame(height: 44).padding(.horizontal, 30)
                if let notice = r.notice {
                    Text(notice).font(.footnote).foregroundStyle(Theme.warn).multilineTextAlignment(.center).padding(.horizontal)
                }
            }
            .padding(.top, 24)

            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 10) {
                        if r.lines.isEmpty && r.liveText.isEmpty {
                            Text("The transcript appears here as you record.").foregroundStyle(Theme.faint)
                        }
                        ForEach(Array(r.lines.enumerated()), id: \.offset) { _, line in
                            TranscriptLineView(line: line, onTap: nil)
                        }
                        if !r.liveText.isEmpty {
                            Text(r.liveText).font(.system(.body, design: .serif)).foregroundStyle(Theme.muted).id("live")
                        }
                        Color.clear.frame(height: 1).id("end")
                    }
                    .padding(20)
                }
                .onChange(of: r.lines.count) { proxy.scrollTo("end") }
                .onChange(of: r.liveText) { proxy.scrollTo("end") }
            }
            .background(Theme.surface, in: .rect(cornerRadius: 24))
            .padding(.horizontal, 12)
            .padding(.top, 20)

            GlassEffectContainer(spacing: 16) {
                HStack(spacing: 16) {
                    Button {
                        r.phase == .paused ? r.resume() : r.pause()
                    } label: {
                        Label(r.phase == .paused ? "Resume" : "Pause", systemImage: r.phase == .paused ? "play.fill" : "pause.fill")
                            .frame(maxWidth: .infinity).padding(.vertical, 6)
                    }
                    .buttonStyle(.glass)
                    .glassEffectID("pause", in: glass)
                    .disabled(r.phase == .finishing || r.phase == .preparing)

                    Button {
                        Task {
                            await model.stopRecording()
                            dismiss()
                        }
                    } label: {
                        Label(r.phase == .finishing ? "Saving…" : "Stop", systemImage: "stop.fill")
                            .frame(maxWidth: .infinity).padding(.vertical, 6)
                    }
                    .buttonStyle(.glassProminent)
                    .tint(Theme.record)
                    .glassEffectID("stop", in: glass)
                    .disabled(r.phase == .finishing || r.phase == .preparing)
                    .accessibilityIdentifier("stop")
                }
                .font(.headline)
            }
            .padding(20)
        }
        .paperBackground()
        .onChange(of: r.phase) { _, phase in if phase == .idle { dismiss() } }
    }
}

struct LevelMeter: View {
    var levels: [Float]
    var active: Bool
    var body: some View {
        GeometryReader { geo in
            HStack(alignment: .center, spacing: 3) {
                ForEach(Array(levels.enumerated()), id: \.offset) { _, level in
                    Capsule()
                        .fill(active ? Theme.ink : Theme.faint)
                        .frame(height: max(3, CGFloat(level) * geo.size.height))
                }
            }
            .frame(maxHeight: .infinity)
            .animation(.linear(duration: 0.1), value: levels)
        }
    }
}

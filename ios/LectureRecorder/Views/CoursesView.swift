import PhotosUI
import SwiftUI
import UniformTypeIdentifiers

struct CoursesView: View {
    @Environment(AppModel.self) private var model
    @State private var adding = false
    @State private var newName = ""

    var body: some View {
        NavigationStack {
            List {
                if model.store.courses.isEmpty {
                    EmptyHint(icon: "books.vertical", title: "No courses yet",
                              message: "Courses group lectures with their slides, readings and handouts, so you can quiz yourself on all of it at once.")
                        .listRowBackground(Color.clear)
                }
                ForEach(model.store.sortedCourses) { course in
                    NavigationLink(value: course.id) {
                        HStack(spacing: 12) {
                            RoundedRectangle(cornerRadius: 8).fill(Theme.courseColor(course.id).opacity(0.9))
                                .frame(width: 36, height: 36)
                                .overlay(Text(String(course.name.prefix(1))).font(.headline).foregroundStyle(.white))
                            VStack(alignment: .leading, spacing: 2) {
                                Text(course.name).font(.body.weight(.semibold))
                                let n = model.store.lectures(in: course).count
                                Text("\(n) lecture\(n == 1 ? "" : "s"), \(course.materials.count) file\(course.materials.count == 1 ? "" : "s")")
                                    .font(.caption).foregroundStyle(Theme.muted)
                            }
                        }
                    }
                    .listRowBackground(Theme.surface)
                }
                .onDelete { offsets in
                    let courses = model.store.sortedCourses
                    for i in offsets { model.store.deleteCourse(courses[i].id) }
                    model.syncSoon()
                }
            }
            .scrollContentBackground(.hidden)
            .paperBackground()
            .navigationTitle("Courses")
            .navigationDestination(for: String.self) { CourseView(courseID: $0) }
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("New course", systemImage: "plus") { newName = ""; adding = true }
                }
            }
            .alert("New course", isPresented: $adding) {
                TextField("e.g. PHYS 201", text: $newName)
                Button("Create") { model.store.ensureCourse(named: newName); model.syncSoon() }
                Button("Cancel", role: .cancel) {}
            }
        }
    }
}

struct CourseView: View {
    @Environment(AppModel.self) private var model
    let courseID: String
    @State private var section = "files"
    @State private var importingFiles = false
    @State private var photos: [PhotosPickerItem] = []
    @State private var renaming = false
    @State private var newName = ""
    @State private var adding = false

    var body: some View {
        if let course = model.store.courses[courseID] {
            Group {
                switch section {
                case "chat": ChatView(scope: .course(courseID))
                case "quiz": QuizView(scope: .course(courseID))
                case "cards": CardsView(scope: .course(courseID))
                default: overview(course)
                }
            }
            .safeAreaInset(edge: .top, spacing: 0) {
                Picker("Section", selection: $section) {
                    Text("Material").tag("files")
                    Text("Chat").tag("chat")
                    Text("Quiz").tag("quiz")
                    Text("Cards").tag("cards")
                }
                .pickerStyle(.segmented)
                .padding(.horizontal, 16).padding(.vertical, 8)
                .glassEffect(.regular, in: .capsule)
                .padding(.horizontal, 12).padding(.bottom, 6)
            }
            .paperBackground()
            .navigationTitle(course.name)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Menu {
                        Button("Rename", systemImage: "pencil") { newName = course.name; renaming = true }
                    } label: { Image(systemName: "ellipsis") }
                }
            }
            .alert("Rename course", isPresented: $renaming) {
                TextField("Name", text: $newName)
                Button("Save") {
                    let name = newName.trimmingCharacters(in: .whitespaces)
                    if !name.isEmpty && model.store.course(named: name) == nil { model.store.renameCourse(courseID, to: name) }
                }
                Button("Cancel", role: .cancel) {}
            }
            .fileImporter(isPresented: $importingFiles, allowedContentTypes: [.pdf, .plainText, .text, .image],
                          allowsMultipleSelection: true) { result in
                guard case .success(let urls) = result else { return }
                add(urls.map { url in { try MaterialImport.read(url: url) } })
            }
            .onChange(of: photos) {
                let items = photos
                photos = []
                Task {
                    var readers: [() throws -> MaterialImport.Result] = []
                    for (i, item) in items.enumerated() {
                        if let data = try? await item.loadTransferable(type: Data.self), let image = UIImage(data: data) {
                            readers.append { try MaterialImport.photo(image, name: "Photo \(i + 1)") }
                        }
                    }
                    add(readers)
                }
            }
        }
    }

    private func add(_ readers: [() throws -> MaterialImport.Result]) {
        var failed: [String] = []
        for read in readers {
            do {
                let r = try read()
                model.store.addMaterial(to: courseID, filename: r.filename, kind: r.kind, data: r.data, text: r.text, pages: r.pages)
            } catch {
                failed.append(error.localizedDescription)
            }
        }
        if !failed.isEmpty { model.errorMessage = failed.joined(separator: "\n") }
        model.syncSoon()
    }

    private func included(_ key: String, _ course: CourseDoc) -> Binding<Bool> {
        Binding(get: { !course.excluded.contains(key) }, set: { on in
            model.store.update(course: courseID) { c in
                if on { c.excluded.removeAll { $0 == key } } else if !c.excluded.contains(key) { c.excluded.append(key) }
            }
        })
    }

    private func overview(_ course: CourseDoc) -> some View {
        List {
            Section {
                Text("Ticked lectures and files are used by this course's Chat, Quiz and Cards.")
                    .font(.footnote).foregroundStyle(Theme.muted).listRowBackground(Color.clear)
            }
            Section("Lectures") {
                let lectures = model.store.lectures(in: course)
                if lectures.isEmpty {
                    Text("Lectures recorded with this course name appear here.").foregroundStyle(Theme.muted)
                }
                ForEach(lectures) { lecture in
                    Toggle(isOn: included("lecture:\(lecture.id)", course)) {
                        VStack(alignment: .leading) {
                            Text(lecture.title)
                            Text(Date(timeIntervalSince1970: lecture.createdAt).formatted(date: .abbreviated, time: .omitted))
                                .font(.caption).foregroundStyle(Theme.muted)
                        }
                    }
                }
            }
            .listRowBackground(Theme.surface)
            Section("Files") {
                ForEach(course.materials) { m in
                    Toggle(isOn: included("material:\(m.id)", course)) {
                        HStack {
                            Image(systemName: icon(m.kind)).foregroundStyle(Theme.muted).frame(width: 24)
                            VStack(alignment: .leading) {
                                Text(m.filename).lineLimit(1)
                                Text(kindLabel(m)).font(.caption).foregroundStyle(Theme.muted)
                            }
                        }
                    }
                    .swipeActions {
                        Button("Remove", role: .destructive) { model.store.removeMaterial(m.id, from: courseID); model.syncSoon() }
                    }
                }
                Menu {
                    Button("From Files", systemImage: "folder") { importingFiles = true }
                    PhotosPicker(selection: $photos, maxSelectionCount: 10, matching: .images) {
                        Label("Photos of handouts or whiteboards", systemImage: "photo")
                    }
                } label: {
                    Label("Add files", systemImage: "plus.circle.fill")
                }
            }
            .listRowBackground(Theme.surface)
        }
        .scrollContentBackground(.hidden)
    }

    private func icon(_ kind: String) -> String {
        switch kind {
        case "image": return "photo"
        case "slides": return "rectangle.on.rectangle.angled"
        case "text": return "doc.plaintext"
        default: return "doc.richtext"
        }
    }

    private func kindLabel(_ m: Material) -> String {
        let kind = ["pdf": "PDF", "pdf_scan": "Scanned PDF", "slides": "Slides", "document": "Word", "text": "Text",
                    "image": "Photo"][m.kind] ?? m.kind
        let pages = m.pages > 0 && m.kind != "image" ? ", \(m.pages) \(m.kind == "slides" ? "slides" : "pages")" : ""
        return "\(kind)\(pages), \(ByteCountFormatter.string(fromByteCount: Int64(m.size), countStyle: .file))"
    }
}

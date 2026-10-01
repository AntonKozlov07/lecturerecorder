import Foundation
import Observation

/// Everything the app knows, kept as JSON files under Documents/Library:
///   lectures/<id>.json, courses/<id>.json, files/<course>/<material>/<name>,
///   audio/<lecture id>.m4a, deleted.json
@MainActor
@Observable
final class LibraryStore {
    private(set) var lectures: [String: LectureDoc] = [:]
    private(set) var courses: [String: CourseDoc] = [:]
    private(set) var tombstones = Tombstones()
    /// Bumped whenever anything changes, so sync knows to run.
    private(set) var revision = 0

    let root: URL

    init(root: URL) {
        self.root = root
        for dir in ["lectures", "courses", "files", "audio"] {
            try? FileManager.default.createDirectory(at: root.appendingPathComponent(dir), withIntermediateDirectories: true)
        }
        load()
    }

    static func defaultRoot() -> URL {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0].appendingPathComponent("Library")
    }

    // MARK: Loading and saving

    private func load() {
        lectures = loadAll(LectureDoc.self, in: "lectures")
        courses = loadAll(CourseDoc.self, in: "courses")
        if let data = try? Data(contentsOf: root.appendingPathComponent("deleted.json")),
           let t = try? DocCoding.decoder.decode(Tombstones.self, from: data) {
            tombstones = t
        }
    }

    private func loadAll<T: Decodable & Identifiable>(_ type: T.Type, in dir: String) -> [String: T] where T.ID == String {
        let folder = root.appendingPathComponent(dir)
        let files = (try? FileManager.default.contentsOfDirectory(at: folder, includingPropertiesForKeys: nil)) ?? []
        var out: [String: T] = [:]
        for url in files where url.pathExtension == "json" {
            if let data = try? Data(contentsOf: url), let doc = try? DocCoding.decoder.decode(T.self, from: data) {
                out[doc.id] = doc
            }
        }
        return out
    }

    private func write<T: Encodable>(_ value: T, to path: String) {
        let url = root.appendingPathComponent(path)
        if let data = try? DocCoding.encoder.encode(value) {
            try? data.write(to: url, options: .atomic)
        }
    }

    // MARK: Lectures

    var sortedLectures: [LectureDoc] { lectures.values.sorted { $0.createdAt > $1.createdAt } }

    func lecture(_ id: String) -> LectureDoc? { lectures[id] }

    func save(_ lecture: LectureDoc) {
        lectures[lecture.id] = lecture
        write(lecture, to: "lectures/\(lecture.id).json")
        if !lecture.course.isEmpty { ensureCourse(named: lecture.course) }
        revision += 1
    }

    func update(_ id: String, _ change: (inout LectureDoc) -> Void) {
        guard var doc = lectures[id] else { return }
        change(&doc)
        save(doc)
    }

    func deleteLecture(_ id: String) {
        lectures[id] = nil
        try? FileManager.default.removeItem(at: root.appendingPathComponent("lectures/\(id).json"))
        try? FileManager.default.removeItem(at: audioURL(for: id))
        tombstones.lectures[id] = now()
        saveTombstones()
        revision += 1
    }

    func audioURL(for lectureID: String) -> URL {
        root.appendingPathComponent("audio/\(lectureID).m4a")
    }

    func hasAudio(_ lectureID: String) -> Bool {
        FileManager.default.fileExists(atPath: audioURL(for: lectureID).path)
    }

    // MARK: Courses

    var sortedCourses: [CourseDoc] {
        courses.values.sorted { $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending }
    }

    func course(named name: String) -> CourseDoc? { courses.values.first { $0.name == name } }

    @discardableResult
    func ensureCourse(named name: String) -> CourseDoc? {
        let name = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty else { return nil }
        if let existing = course(named: name) { return existing }
        let course = CourseDoc(id: newID(), name: name, createdAt: now())
        save(course)
        return course
    }

    func save(_ course: CourseDoc) {
        courses[course.id] = course
        write(course, to: "courses/\(course.id).json")
        revision += 1
    }

    func update(course id: String, _ change: (inout CourseDoc) -> Void) {
        guard var doc = courses[id] else { return }
        change(&doc)
        save(doc)
    }

    func renameCourse(_ id: String, to newName: String) {
        guard let old = courses[id]?.name else { return }
        update(course: id) { $0.name = newName }
        for lecture in lectures.values where lecture.course == old {
            update(lecture.id) { $0.course = newName }
        }
    }

    func deleteCourse(_ id: String) {
        guard let course = courses[id] else { return }
        for lecture in lectures.values where lecture.course == course.name {
            update(lecture.id) { $0.course = "" }
        }
        courses[id] = nil
        try? FileManager.default.removeItem(at: root.appendingPathComponent("courses/\(id).json"))
        try? FileManager.default.removeItem(at: root.appendingPathComponent("files/\(id)"))
        tombstones.courses[id] = now()
        saveTombstones()
        revision += 1
    }

    func lectures(in course: CourseDoc) -> [LectureDoc] {
        lectures.values.filter { $0.course == course.name }.sorted { $0.createdAt < $1.createdAt }
    }

    // MARK: Course files

    func fileURL(_ repoPath: String) -> URL { root.appendingPathComponent(repoPath) }

    func materialURL(_ material: Material) -> URL? {
        guard let file = material.file else { return nil }
        let url = fileURL(file)
        return FileManager.default.fileExists(atPath: url.path) ? url : nil
    }

    func addMaterial(to courseID: String, filename: String, kind: String, data: Data, text: String, pages: Int) {
        let id = newID()
        let safe = filename.replacingOccurrences(of: "/", with: "_")
        let path = "files/\(courseID)/\(id)/\(safe)"
        let url = fileURL(path)
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? data.write(to: url)
        let material = Material(id: id, filename: filename, kind: kind, pages: pages, size: data.count,
                                createdAt: now(), text: text, file: data.count <= 25 * 1024 * 1024 ? path : nil)
        update(course: courseID) { $0.materials.append(material) }
    }

    func removeMaterial(_ materialID: String, from courseID: String) {
        update(course: courseID) { course in
            if let m = course.materials.first(where: { $0.id == materialID }), let file = m.file {
                try? FileManager.default.removeItem(at: fileURL(file).deletingLastPathComponent())
            }
            course.materials.removeAll { $0.id == materialID }
        }
        tombstones.materials[materialID] = now()
        saveTombstones()
    }

    // MARK: Sync support

    func saveTombstones() {
        write(tombstones, to: "deleted.json")
    }

    /// Replace a document with one that came from sync (no tombstone, keeps the store consistent).
    func importLecture(_ doc: LectureDoc) {
        lectures[doc.id] = doc
        write(doc, to: "lectures/\(doc.id).json")
    }

    func importCourse(_ doc: CourseDoc) {
        courses[doc.id] = doc
        write(doc, to: "courses/\(doc.id).json")
    }

    /// Apply deletions made on the other device.
    func applyTombstones(_ theirs: Tombstones) {
        for (id, ts) in theirs.lectures where tombstones.lectures[id] == nil {
            tombstones.lectures[id] = ts
            if lectures[id] != nil {
                lectures[id] = nil
                try? FileManager.default.removeItem(at: root.appendingPathComponent("lectures/\(id).json"))
                try? FileManager.default.removeItem(at: audioURL(for: id))
            }
        }
        for (id, ts) in theirs.courses where tombstones.courses[id] == nil {
            tombstones.courses[id] = ts
            if let course = courses[id] {
                for lecture in lectures.values where lecture.course == course.name {
                    var l = lecture
                    l.course = ""
                    importLecture(l)
                }
                courses[id] = nil
                try? FileManager.default.removeItem(at: root.appendingPathComponent("courses/\(id).json"))
                try? FileManager.default.removeItem(at: root.appendingPathComponent("files/\(id)"))
            }
        }
        for (id, ts) in theirs.materials where tombstones.materials[id] == nil {
            tombstones.materials[id] = ts
            for course in courses.values where course.materials.contains(where: { $0.id == id }) {
                var c = course
                c.materials.removeAll { $0.id == id }
                importCourse(c)
            }
        }
        saveTombstones()
    }

    /// Give a never-synced local course the ID of the same-named course from the other device.
    func adoptCourseID(local: String, remote: String) {
        guard var course = courses[local] else { return }
        courses[local] = nil
        try? FileManager.default.removeItem(at: root.appendingPathComponent("courses/\(local).json"))
        course.id = remote
        importCourse(course)
    }

    func didChange() { revision += 1 }

    /// Remove everything (used by UI tests).
    func reset() {
        try? FileManager.default.removeItem(at: root)
        for dir in ["lectures", "courses", "files", "audio"] {
            try? FileManager.default.createDirectory(at: root.appendingPathComponent(dir), withIntermediateDirectories: true)
        }
        lectures = [:]
        courses = [:]
        tombstones = Tombstones()
        revision += 1
    }
}

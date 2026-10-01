import CryptoKit
import Foundation

/// Sync with the PC app through a private GitHub repository.
/// Same contract as lecturerecorder/sync.py; see docs/sync-format.md.
@MainActor
final class GitHubSync {
    struct Stats: Equatable { var uploaded = 0, downloaded = 0, merged = 0 }

    struct SyncError: LocalizedError {
        let message: String
        let conflict: Bool
        init(_ message: String, conflict: Bool = false) { self.message = message; self.conflict = conflict }
        var errorDescription: String? { message }
    }

    private struct FileState: Codable { var remoteSha = ""; var localHash = ""; var modifiedAt: Double = 0 }

    static let marker = "lecture-recorder.json"
    static let tombstonesPath = "deleted.json"
    static let maxFile = 25 * 1024 * 1024

    let store: LibraryStore
    let repo: String
    let token: String
    let api: URL
    /// Lectures still being recorded or processed on this phone; they sync once finished.
    var busyLectures: Set<String> = []

    private var state: [String: FileState] = [:]
    private var stateURL: URL { store.root.appendingPathComponent("sync-state.json") }

    init(store: LibraryStore, repo: String, token: String, api: URL? = nil) {
        self.store = store
        self.repo = repo
        self.token = token
        let override = UserDefaults.standard.string(forKey: "github_api").flatMap(URL.init(string:))
        self.api = api ?? override ?? URL(string: "https://api.github.com")!
        if let data = try? Data(contentsOf: stateURL),
           let saved = try? DocCoding.decoder.decode([String: FileState].self, from: data) {
            state = saved
        }
    }

    // MARK: GitHub API

    private func call(_ method: String, _ path: String, _ body: [String: Any]? = nil, allowMissing: Bool = false) async throws -> [String: Any]? {
        var req = URLRequest(url: URL(string: "\(api.absoluteString)/repos/\(repo)\(path)")!)
        req.httpMethod = method
        req.timeoutInterval = 60
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        req.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        req.setValue("2022-11-28", forHTTPHeaderField: "X-GitHub-Api-Version")
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let data: Data, response: URLResponse
        do {
            (data, response) = try await URLSession.shared.data(for: req)
        } catch {
            throw SyncError("Can't reach GitHub. Check your internet connection.")
        }
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        if (200..<300).contains(status) {
            return data.isEmpty ? nil : (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        }
        let detail = String(decoding: data.prefix(300), as: UTF8.self)
        if allowMissing && (status == 404 || status == 409) { return nil }
        switch status {
        case 401: throw SyncError("GitHub rejected the token. Create a new one and paste it in Settings.")
        case 403 where detail.lowercased().contains("rate limit"):
            throw SyncError("GitHub's rate limit was reached. Sync will try again later.")
        case 403, 404:
            throw SyncError("Can't access that repository. Check its name, and that the token has Contents: Read and write access to it.")
        case 422: throw SyncError(detail, conflict: true)
        default: throw SyncError("GitHub error \(status): \(detail)")
        }
    }

    private func blob(_ sha: String) async throws -> Data {
        guard let obj = try await call("GET", "/git/blobs/\(sha)"), let content = obj["content"] as? String,
              let data = Data(base64Encoded: content, options: .ignoreUnknownCharacters) else {
            throw SyncError("Couldn't download a file from GitHub.")
        }
        return data
    }

    static func gitBlobSha(_ data: Data) -> String {
        var hasher = Insecure.SHA1()
        hasher.update(data: Data("blob \(data.count)\0".utf8))
        hasher.update(data: data)
        return hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }

    static func hash<T: Encodable>(_ doc: T) -> String {
        let data = (try? DocCoding.hashEncoder.encode(doc)) ?? Data()
        return SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    static func lectureHash(_ doc: LectureDoc) -> String { var d = doc; d.modifiedAt = nil; return hash(d) }
    static func courseHash(_ doc: CourseDoc) -> String { var d = doc; d.modifiedAt = nil; return hash(d) }

    // MARK: Merge (docs/sync-format.md, "Merging")

    private static func key(_ createdAt: Double, _ extra: String) -> String {
        "\((createdAt * 1000).rounded() / 1000)|\(extra)"
    }

    static func union<T>(_ older: [T], _ newer: [T], key: (T) -> String, createdAt: (T) -> Double,
                         prefer: ((T, T) -> T)? = nil) -> [T] {
        var out: [String: T] = [:]
        for item in older + newer {
            let k = key(item)
            if let existing = out[k], let prefer { out[k] = prefer(existing, item) } else { out[k] = item }
        }
        return out.values.sorted { createdAt($0) < createdAt($1) }
    }

    private static func preferQuiz(_ old: Quiz, _ new: Quiz) -> Quiz {
        let a = old.answers != nil, b = new.answers != nil
        if a != b { return a ? old : new }
        return new
    }

    static func merge(_ local: LectureDoc, _ remote: LectureDoc) -> LectureDoc {
        let remoteNewer = (remote.modifiedAt ?? 0) >= (local.modifiedAt ?? 0)
        let (newer, older) = remoteNewer ? (remote, local) : (local, remote)
        var m = newer
        m.chat = union(older.chat, newer.chat, key: { key($0.createdAt, $0.role) }, createdAt: { $0.createdAt })
        m.quizzes = union(older.quizzes, newer.quizzes, key: { key($0.createdAt, $0.kind) }, createdAt: { $0.createdAt },
                          prefer: preferQuiz)
        m.deepDives = union(older.deepDives, newer.deepDives, key: { key($0.createdAt, $0.topic) }, createdAt: { $0.createdAt })
        return m
    }

    static func merge(_ local: CourseDoc, _ remote: CourseDoc) -> CourseDoc {
        let remoteNewer = (remote.modifiedAt ?? 0) >= (local.modifiedAt ?? 0)
        let (newer, older) = remoteNewer ? (remote, local) : (local, remote)
        var m = newer
        m.chat = union(older.chat, newer.chat, key: { key($0.createdAt, $0.role) }, createdAt: { $0.createdAt })
        m.quizzes = union(older.quizzes, newer.quizzes, key: { key($0.createdAt, $0.kind) }, createdAt: { $0.createdAt },
                          prefer: preferQuiz)
        var mats = Dictionary(older.materials.map { ($0.id, $0) }, uniquingKeysWith: { $1 })
        for mat in newer.materials { mats[mat.id] = mat }
        m.materials = mats.values.sorted { $0.createdAt < $1.createdAt }
        return m
    }

    // MARK: The sync run

    func run() async throws -> Stats {
        for attempt in 0..<3 {
            do {
                return try await syncOnce()
            } catch let error as SyncError where error.conflict && attempt < 2 {
                try? await Task.sleep(for: .seconds(1))  // the PC pushed at the same moment: start over
            }
        }
        throw SyncError("Sync kept colliding with the other device. Try again in a moment.")
    }

    private func syncOnce() async throws -> Stats {
        guard let repoInfo = try await call("GET", ""), let branch = repoInfo["default_branch"] as? String else {
            throw SyncError("Couldn't read the repository.")
        }
        var ref = try await call("GET", "/git/ref/heads/\(branch)", allowMissing: true)
        if ref == nil {
            let marker = try DocCoding.encoder.encode(["format": 1])
            _ = try await call("PUT", "/contents/\(Self.marker)",
                               ["message": "Set up Lecture Recorder sync", "content": marker.base64EncodedString()])
            ref = try await call("GET", "/git/ref/heads/\(branch)")
        }
        guard let head = (ref?["object"] as? [String: Any])?["sha"] as? String,
              let commit = try await call("GET", "/git/commits/\(head)"),
              let baseTree = (commit["tree"] as? [String: Any])?["sha"] as? String,
              let tree = try await call("GET", "/git/trees/\(baseTree)?recursive=1") else {
            throw SyncError("Couldn't read the repository's files.")
        }
        var remote: [String: String] = [:]
        for entry in tree["tree"] as? [[String: Any]] ?? [] where entry["type"] as? String == "blob" {
            if let path = entry["path"] as? String, let sha = entry["sha"] as? String { remote[path] = sha }
        }
        currentRemote = remote
        let allowed: Set<String> = [Self.marker, Self.tombstonesPath, "README.md", ".gitignore", "LICENSE"]
        let foreign = remote.keys.filter { !($0.hasPrefix("lectures/") || $0.hasPrefix("courses/") || $0.hasPrefix("files/") || allowed.contains($0)) }
        if !foreign.isEmpty && remote[Self.marker] == nil {
            throw SyncError("That repository already has other files in it. Use a new, empty private repository.")
        }

        var fetched: [String: Data] = [:]
        func fetch(_ path: String) async throws -> Data? {
            guard let sha = remote[path] else { return nil }
            if let d = fetched[path] { return d }
            let d = try await blob(sha)
            fetched[path] = d
            return d
        }

        var uploads: [String: Data] = [:]
        var deletes: Set<String> = []
        if remote[Self.marker] == nil { uploads[Self.marker] = try DocCoding.encoder.encode(["format": 1]) }

        // Tombstones first, so nothing deleted elsewhere is uploaded again.
        if let data = try await fetch(Self.tombstonesPath),
           let theirs = try? DocCoding.decoder.decode(Tombstones.self, from: data) {
            store.applyTombstones(theirs)
        }
        let tomb = store.tombstones
        let tombData = try DocCoding.encoder.encode(tomb)
        if tomb != Tombstones(), Self.gitBlobSha(tombData) != remote[Self.tombstonesPath] {
            uploads[Self.tombstonesPath] = tombData
        }
        for id in tomb.lectures.keys where remote["lectures/\(id).json"] != nil { deletes.insert("lectures/\(id).json") }
        for id in tomb.courses.keys where remote["courses/\(id).json"] != nil { deletes.insert("courses/\(id).json") }
        for path in remote.keys where path.hasPrefix("files/") {
            let parts = path.split(separator: "/").map(String.init)
            if parts.count >= 3 && (tomb.courses[parts[1]] != nil || tomb.materials[parts[2]] != nil) { deletes.insert(path) }
        }

        var stats = Stats()
        var newStates: [String: FileState] = [:]

        // Courses first, so downloaded lectures find their course.
        let localCourseIDs = Set(store.courses.keys)
        let remoteCourseIDs = Set(remote.keys.filter { $0.hasPrefix("courses/") && $0.hasSuffix(".json") }
            .map { String($0.dropFirst(8).dropLast(5)) })
        for id in localCourseIDs.union(remoteCourseIDs).subtracting(tomb.courses.keys).sorted() {
            let path = "courses/\(id).json"
            var local = store.courses[id]
            // The same course created on both devices before they ever synced: join them.
            if local == nil, remote[path] != nil, let data = try await fetch(path),
               let theirs = try? DocCoding.decoder.decode(CourseDoc.self, from: data),
               let clash = store.course(named: theirs.name), state["courses/\(clash.id).json"] == nil {
                store.adoptCourseID(local: clash.id, remote: id)
                local = store.courses[id]
            }
            let outcome = try await reconcile(path: path, local: local, hash: { Self.courseHash($0) }, fetch: fetch,
                                              merge: { Self.merge($0, $1) }, importer: { try await self.importCourse($0, fetch: fetch) },
                                              uploads: &uploads)
            if let outcome { newStates[path] = outcome.0; Self.count(outcome.1, into: &stats) }
            if let course = store.courses[id] {
                for m in course.materials {
                    if let file = m.file, remote[file] == nil, let url = store.materialURL(m), let data = try? Data(contentsOf: url) {
                        uploads[file] = data
                    }
                }
            }
        }

        let localLectureIDs = Set(store.lectures.keys)
        let remoteLectureIDs = Set(remote.keys.filter { $0.hasPrefix("lectures/") && $0.hasSuffix(".json") }
            .map { String($0.dropFirst(9).dropLast(5)) })
        for id in localLectureIDs.union(remoteLectureIDs).subtracting(tomb.lectures.keys).sorted() where !busyLectures.contains(id) {
            let path = "lectures/\(id).json"
            let outcome = try await reconcile(path: path, local: store.lectures[id], hash: { Self.lectureHash($0) },
                                              fetch: fetch, merge: { Self.merge($0, $1) },
                                              importer: { self.store.importLecture($0) }, uploads: &uploads)
            if let outcome { newStates[path] = outcome.0; Self.count(outcome.1, into: &stats) }
        }

        if !uploads.isEmpty || !deletes.isEmpty {
            var entries: [[String: Any]] = []
            for (path, data) in uploads {
                guard let b = try await call("POST", "/git/blobs", ["content": data.base64EncodedString(), "encoding": "base64"]),
                      let sha = b["sha"] as? String else { throw SyncError("Couldn't upload to GitHub.") }
                entries.append(["path": path, "mode": "100644", "type": "blob", "sha": sha])
            }
            for path in deletes where uploads[path] == nil {
                entries.append(["path": path, "mode": "100644", "type": "blob", "sha": NSNull()])
            }
            guard let newTree = try await call("POST", "/git/trees", ["base_tree": baseTree, "tree": entries]),
                  let treeSha = newTree["sha"] as? String,
                  let newCommit = try await call("POST", "/git/commits", ["message": "Sync from iPhone", "tree": treeSha, "parents": [head]]),
                  let commitSha = newCommit["sha"] as? String else {
                throw SyncError("Couldn't save to GitHub.")
            }
            _ = try await call("PATCH", "/git/refs/heads/\(branch)", ["sha": commitSha, "force": false])
        }

        // Remember what each synced document looks like now, as stored on this phone.
        for (path, var st) in newStates {
            if path.hasPrefix("courses/"), let doc = store.courses[String(path.dropFirst(8).dropLast(5))] {
                st.localHash = Self.courseHash(doc)
            } else if path.hasPrefix("lectures/"), let doc = store.lectures[String(path.dropFirst(9).dropLast(5))] {
                st.localHash = Self.lectureHash(doc)
            }
            state[path] = st
        }
        for path in deletes { state[path] = nil }
        try? DocCoding.encoder.encode(state).write(to: stateURL, options: .atomic)
        return stats
    }

    private enum Outcome { case uploaded, downloaded, merged }

    private static func count(_ o: Outcome, into s: inout Stats) {
        switch o {
        case .uploaded: s.uploaded += 1
        case .downloaded: s.downloaded += 1
        case .merged: s.merged += 1
        }
    }

    /// Decide what to do with one document (see docs/sync-format.md, "Sync algorithm").
    private func reconcile<T: Codable & ModifiedDocument>(
        path: String, local: T?, hash: (T) -> String, fetch: (String) async throws -> Data?,
        merge: (T, T) -> T, importer: (T) async throws -> Void, uploads: inout [String: Data]
    ) async throws -> (FileState, Outcome)? {
        let st = state[path] ?? FileState()
        let localHash = local.map(hash) ?? ""
        var localChanged = local != nil && localHash != st.localHash
        let remoteSha = currentRemote[path]
        let remoteChanged = remoteSha != nil && remoteSha != st.remoteSha
        if local != nil && remoteSha == nil { localChanged = true }
        if !localChanged && !remoteChanged { return nil }
        if localChanged && !remoteChanged, var doc = local {
            doc.modifiedAt = now()
            let data = try DocCoding.encoder.encode(doc)
            uploads[path] = data
            return (FileState(remoteSha: Self.gitBlobSha(data), localHash: "", modifiedAt: doc.modifiedAt ?? 0), .uploaded)
        }
        guard let data = try await fetch(path) else { return nil }
        let theirs = try DocCoding.decoder.decode(T.self, from: data)
        if theirs.format > 1 { throw SyncError("The PC app is newer than this app. Update Lecture Recorder on your iPhone.") }
        if localChanged, var mine = local {
            mine.modifiedAt = now()
            let merged = merge(mine, theirs)
            try await importer(merged)
            let out = try DocCoding.encoder.encode(merged)
            uploads[path] = out
            return (FileState(remoteSha: Self.gitBlobSha(out), localHash: "", modifiedAt: merged.modifiedAt ?? 0), .merged)
        }
        try await importer(theirs)
        return (FileState(remoteSha: remoteSha ?? "", localHash: "", modifiedAt: theirs.modifiedAt ?? 0), .downloaded)
    }

    /// Blob SHAs of every file on GitHub at the start of the current sync.
    private var currentRemote: [String: String] = [:]

    private func importCourse(_ doc: CourseDoc, fetch: (String) async throws -> Data?) async throws {
        var doc = doc
        if let clash = store.course(named: doc.name), clash.id != doc.id { doc.name += " (2)" }
        let old = store.courses[doc.id]
        // Download files this phone doesn't have yet, and drop ones that were removed.
        for m in doc.materials {
            guard let file = m.file, store.materialURL(m) == nil, let data = try await fetch(file) else { continue }
            let url = store.fileURL(file)
            try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            try? data.write(to: url)
        }
        let keep = Set(doc.materials.map(\.id))
        for m in old?.materials ?? [] where !keep.contains(m.id) {
            if let url = store.materialURL(m) { try? FileManager.default.removeItem(at: url.deletingLastPathComponent()) }
        }
        if let old, old.name != doc.name {
            for lecture in store.lectures.values where lecture.course == old.name {
                var l = lecture
                l.course = doc.name
                store.importLecture(l)
            }
        }
        store.importCourse(doc)
    }
}

/// Lecture and course documents both carry a format version and a modified time.
protocol ModifiedDocument {
    var modifiedAt: Double? { get set }
    var format: Int { get }
}

extension LectureDoc: ModifiedDocument {}
extension CourseDoc: ModifiedDocument {}

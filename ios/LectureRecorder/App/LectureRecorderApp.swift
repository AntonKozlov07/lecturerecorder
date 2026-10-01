import SwiftUI

@main
struct LectureRecorderApp: App {
    @State private var model: AppModel

    init() {
        let args = ProcessInfo.processInfo.arguments
        if args.contains("-uitest") {
            // UI tests run against a separate library and settings so they never touch real data.
            let root = FileManager.default.temporaryDirectory.appendingPathComponent("uitest-library")
            let defaults = UserDefaults(suiteName: "uitest")!
            defaults.removePersistentDomain(forName: "uitest")
            let model = AppModel(store: LibraryStore(root: root), settings: AppSettings(defaults: defaults))
            if args.contains("-sample") { model.loadSampleData() } else { model.store.reset() }
            _model = State(initialValue: model)
        } else {
            _model = State(initialValue: AppModel())
        }
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(model)
                .tint(Theme.accent)
                .task { model.startSyncLoop() }
        }
    }
}

struct RootView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.scenePhase) private var scenePhase
    @State private var tab = "lectures"

    var body: some View {
        @Bindable var model = model
        TabView(selection: $tab) {
            Tab("Lectures", systemImage: "waveform", value: "lectures") {
                LibraryView()
            }
            Tab("Courses", systemImage: "books.vertical", value: "courses") {
                CoursesView()
            }
            Tab("Settings", systemImage: "gearshape", value: "settings") {
                SettingsView()
            }
        }
        .tabBarMinimizeBehavior(.onScrollDown)
        .tabViewBottomAccessory {
            if model.recorder.isActive {
                RecordingAccessory()
            } else if model.syncing {
                Label("Syncing with your PC", systemImage: "arrow.triangle.2.circlepath")
                    .font(.subheadline).foregroundStyle(Theme.muted)
                    .frame(maxWidth: .infinity)
            }
        }
        .alert("Something went wrong", isPresented: Binding(get: { model.errorMessage != nil },
                                                           set: { if !$0 { model.errorMessage = nil } })) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(model.errorMessage ?? "")
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { Task { await model.sync() } }
        }
    }
}

/// The live recording strip attached to the tab bar (Liquid Glass), visible from every tab.
struct RecordingAccessory: View {
    @Environment(AppModel.self) private var model
    @State private var showRecorder = false

    var body: some View {
        let r = model.recorder
        HStack(spacing: 12) {
            Circle().fill(r.phase == .recording ? Theme.record : Theme.faint).frame(width: 9, height: 9)
                .opacity(r.phase == .recording ? 1 : 0.6)
            Text(formatTimestamp(r.elapsed)).font(.body.monospacedDigit().weight(.semibold))
            Text(r.phase == .paused ? "Paused" : "Recording").foregroundStyle(Theme.muted).font(.subheadline)
            Spacer()
            Button {
                r.phase == .paused ? r.resume() : r.pause()
            } label: {
                Image(systemName: r.phase == .paused ? "play.fill" : "pause.fill")
            }
            .accessibilityLabel(r.phase == .paused ? "Resume" : "Pause")
        }
        .padding(.horizontal, 16)
        .contentShape(Rectangle())
        .onTapGesture { showRecorder = true }
        .fullScreenCover(isPresented: $showRecorder) { RecordingView() }
    }
}

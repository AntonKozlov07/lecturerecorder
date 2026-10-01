import SwiftUI

struct SettingsView: View {
    @Environment(AppModel.self) private var model
    @State private var apiKeyDraft = ""
    @State private var tokenDraft = ""
    @State private var repoDraft = ""

    var body: some View {
        @Bindable var settings = model.settings
        NavigationStack {
            Form {
                Section {
                    SecureField(settings.aiReady ? "Saved. Paste a new key to replace it." : "sk-ant-...", text: $apiKeyDraft)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                        .onSubmit { saveKey() }
                    if !apiKeyDraft.isEmpty { Button("Save key", action: saveKey) }
                    Picker("Model", selection: $settings.model) {
                        ForEach(AppSettings.models, id: \.id) { Text($0.label).tag($0.id) }
                    }
                    Toggle("Write notes after recording", isOn: $settings.autoNotes)
                    Toggle(isOn: $settings.filterSideTalk) {
                        VStack(alignment: .leading) {
                            Text("Detect side talk")
                            Text("Chatter that isn't part of the lecture is left out of notes, chat and quizzes. Uses Claude Haiku, a few cents per hour.")
                                .font(.caption).foregroundStyle(Theme.muted)
                        }
                    }
                } header: {
                    Text("AI")
                } footer: {
                    Text("Your Anthropic API key is stored in the iPhone's Keychain. Create one at console.anthropic.com.")
                }

                Section {
                    TextField("Language (blank = automatic)", text: $settings.language)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                } header: {
                    Text("Transcription")
                } footer: {
                    Text("Transcripts are made on this iPhone by Apple's speech engine; audio never leaves the phone. Use a code like en, de or es to force a language.")
                }

                Section {
                    LabeledContent("Status") {
                        if model.syncing { ProgressView() }
                        else if let error = model.syncError { Text(error).foregroundStyle(Theme.danger).multilineTextAlignment(.trailing) }
                        else if let last = model.lastSync { Text("Synced \(last.formatted(.relative(presentation: .named)))") }
                        else { Text(settings.syncConfigured ? "Not synced yet" : "Not set up").foregroundStyle(Theme.muted) }
                    }
                    TextField("your-username/lecture-sync", text: $repoDraft)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                        .onSubmit { settings.githubRepo = AppSettings.normalizeRepo(repoDraft) }
                    SecureField(settings.githubToken.isEmpty ? "Access token (github_pat_...)" : "Token saved. Paste a new one to replace it.",
                                text: $tokenDraft)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                    Button("Save and sync now") {
                        settings.githubRepo = AppSettings.normalizeRepo(repoDraft)
                        if !tokenDraft.isEmpty { settings.githubToken = tokenDraft.trimmingCharacters(in: .whitespaces); tokenDraft = "" }
                        Task { await model.sync() }
                    }
                    .disabled(repoDraft.isEmpty || (settings.githubToken.isEmpty && tokenDraft.isEmpty) || model.syncing)
                    DisclosureGroup("How to set up sync") {
                        VStack(alignment: .leading, spacing: 8) {
                            Text("1. On github.com, create a new **private** repository (for example lecture-sync).")
                            Text("2. Settings > Developer settings > Personal access tokens > Fine-grained tokens > Generate new token.")
                            Text("3. Repository access: **Only select repositories**, pick the new one. Permissions: **Contents: Read and write**.")
                            Text("4. Paste the repository name and the token here, and the same two into the PC app's Sync window.")
                        }
                        .font(.footnote)
                    }
                } header: {
                    Text("Sync with your PC")
                } footer: {
                    Text("Lectures, notes, chats, quizzes, flashcards and course files sync through your private GitHub repository. Audio stays on the device that recorded it.")
                }

                Section("Appearance") {
                    Picker("Theme", selection: $settings.appearance) {
                        Text("Match iPhone").tag("system")
                        Text("Light").tag("light")
                        Text("Dark").tag("dark")
                    }
                    .pickerStyle(.segmented)
                    .accessibilityIdentifier("appearance")
                }

                Section {
                    LabeledContent("Version", value: Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "")
                }
            }
            .scrollContentBackground(.hidden)
            .paperBackground()
            .navigationTitle("Settings")
            .onAppear { repoDraft = settings.githubRepo }
        }
    }

    private func saveKey() {
        let key = apiKeyDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        if !key.isEmpty { model.settings.apiKey = key }
        apiKeyDraft = ""
    }
}

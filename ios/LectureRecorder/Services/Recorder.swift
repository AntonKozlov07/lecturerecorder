import AVFoundation
import Foundation
import Observation
import Speech

/// Audio-thread side of recording: writes the file and feeds the speech analyzer.
/// Kept out of the main actor because the tap callback runs on a real-time audio thread.
final class AudioPipe: @unchecked Sendable {
    private let lock = NSLock()
    private var file: AVAudioFile?
    private var converter: AVAudioConverter?
    private var analyzerFormat: AVAudioFormat?
    private var input: AsyncStream<AnalyzerInput>.Continuation?
    private var _paused = false
    private var _level: Float = 0

    var paused: Bool {
        get { lock.withLock { _paused } }
        set { lock.withLock { _paused = newValue } }
    }

    var level: Float { lock.withLock { _level } }

    func configure(file: AVAudioFile?, analyzerFormat: AVAudioFormat?, sourceFormat: AVAudioFormat,
                   input: AsyncStream<AnalyzerInput>.Continuation?) {
        lock.withLock {
            self.file = file
            self.analyzerFormat = analyzerFormat
            self.input = input
            if let analyzerFormat, analyzerFormat != sourceFormat {
                converter = AVAudioConverter(from: sourceFormat, to: analyzerFormat)
            } else {
                converter = nil
            }
        }
    }

    func process(_ buffer: AVAudioPCMBuffer) {
        lock.lock()
        defer { lock.unlock() }
        if let data = buffer.floatChannelData?[0] {
            var sum: Float = 0
            for i in 0..<Int(buffer.frameLength) { sum += data[i] * data[i] }
            _level = min(1, sqrt(sum / Float(max(1, buffer.frameLength))) * 4)
        }
        guard !_paused else { _level = 0; return }
        try? file?.write(from: buffer)
        guard let input else { return }
        if let converter, let analyzerFormat {
            let ratio = analyzerFormat.sampleRate / buffer.format.sampleRate
            let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 32
            guard let out = AVAudioPCMBuffer(pcmFormat: analyzerFormat, frameCapacity: capacity) else { return }
            var consumed = false
            var error: NSError?
            converter.convert(to: out, error: &error) { _, status in
                if consumed { status.pointee = .noDataNow; return nil }
                consumed = true
                status.pointee = .haveData
                return buffer
            }
            if error == nil, out.frameLength > 0 { input.yield(AnalyzerInput(buffer: out)) }
        } else {
            input.yield(AnalyzerInput(buffer: buffer))
        }
    }

    func finishInput() {
        lock.withLock {
            input?.finish()
            input = nil
            file = nil
        }
    }
}

/// Records a lecture: microphone to an .m4a file, with live on-device transcription.
/// Keeps going with the screen locked (the app has the "audio" background mode).
@MainActor
@Observable
final class Recorder {
    enum Phase: Equatable { case idle, preparing, recording, paused, finishing }

    private(set) var phase: Phase = .idle
    private(set) var elapsed: Double = 0
    private(set) var levels: [Float] = Array(repeating: 0, count: 48)
    private(set) var lines: [TranscriptLine] = []
    private(set) var liveText = ""
    private(set) var notice: String?
    private(set) var lectureID: String?

    private let engine = AVAudioEngine()
    private let pipe = AudioPipe()
    private var analyzer: SpeechAnalyzer?
    private var resultsTask: Task<Void, Never>?
    private var clock: Task<Void, Never>?
    private var accumulated: Double = 0
    private var resumedAt: Date?
    private var interruptionObserver: NSObjectProtocol?

    var isActive: Bool { phase != .idle }

    enum RecorderError: LocalizedError {
        case message(String)
        var errorDescription: String? { if case .message(let m) = self { return m }; return nil }
    }

    func start(lectureID: String, audioURL: URL, language: String) async throws {
        guard phase == .idle else { return }
        phase = .preparing
        notice = nil
        lines = []
        liveText = ""
        accumulated = 0
        elapsed = 0
        do {
            guard await AVAudioApplication.requestRecordPermission() else {
                throw RecorderError.message("Microphone access is off. Turn it on in Settings > Privacy & Security > Microphone.")
            }
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.record, mode: .default, options: [.allowBluetoothHFP])
            try session.setActive(true)

            let input = engine.inputNode
            let format = input.outputFormat(forBus: 0)
            guard format.sampleRate > 0 else { throw RecorderError.message("No microphone is available.") }

            let settings: [String: Any] = [AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: format.sampleRate,
                                           AVNumberOfChannelsKey: format.channelCount, AVEncoderBitRateKey: 64000]
            let file = try AVAudioFile(forWriting: audioURL, settings: settings, commonFormat: format.commonFormat,
                                       interleaved: format.isInterleaved)

            let (stream, builder, analyzerFormat) = try await startTranscription(language: language)
            pipe.configure(file: file, analyzerFormat: analyzerFormat, sourceFormat: format, input: builder)
            pipe.paused = false
            input.removeTap(onBus: 0)
            let pipe = self.pipe
            input.installTap(onBus: 0, bufferSize: 4096, format: format) { buffer, _ in pipe.process(buffer) }
            engine.prepare()
            try engine.start()
            _ = stream
            self.lectureID = lectureID
            resumedAt = Date()
            phase = .recording
            observeInterruptions()
            startClock()
        } catch {
            stopEverything()
            phase = .idle
            throw error
        }
    }

    /// Sets up on-device transcription. If this iPhone can't transcribe, recording still works.
    private func startTranscription(language: String) async throws
        -> (AsyncStream<AnalyzerInput>?, AsyncStream<AnalyzerInput>.Continuation?, AVAudioFormat?) {
        guard SpeechTranscriber.isAvailable else {
            notice = "This iPhone can't transcribe on the device, so only audio is being recorded."
            return (nil, nil, nil)
        }
        let wanted = language.isEmpty ? Locale.current : Locale(identifier: language)
        guard let locale = await SpeechTranscriber.supportedLocale(equivalentTo: wanted) else {
            notice = "Transcription doesn't support \(wanted.identifier). Only audio is being recorded."
            return (nil, nil, nil)
        }
        let transcriber = SpeechTranscriber(locale: locale, transcriptionOptions: [],
                                            reportingOptions: [.volatileResults], attributeOptions: [.audioTimeRange])
        do {
            if let request = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
                notice = "Downloading the speech model for \(locale.identifier). This happens once."
                try await request.downloadAndInstall()
                notice = nil
            }
        } catch {
            notice = "The speech model couldn't be downloaded, so only audio is being recorded."
            return (nil, nil, nil)
        }
        let analyzer = SpeechAnalyzer(modules: [transcriber])
        let format = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [transcriber])
        let (stream, builder) = AsyncStream<AnalyzerInput>.makeStream()
        try await analyzer.start(inputSequence: stream)
        self.analyzer = analyzer
        resultsTask = Task { [weak self] in
            do {
                for try await result in transcriber.results {
                    let text = String(result.text.characters).trimmingCharacters(in: .whitespacesAndNewlines)
                    await MainActor.run {
                        guard let self else { return }
                        if result.isFinal {
                            if !text.isEmpty {
                                self.lines.append(TranscriptLine(t: result.range.start.seconds,
                                                                 end: result.range.end.seconds, text: text))
                            }
                            self.liveText = ""
                        } else {
                            self.liveText = text
                        }
                    }
                }
            } catch {
                await MainActor.run { self?.notice = "Transcription stopped: \(error.localizedDescription)" }
            }
        }
        return (stream, builder, format)
    }

    func pause() {
        guard phase == .recording else { return }
        pipe.paused = true
        accumulated += Date().timeIntervalSince(resumedAt ?? Date())
        resumedAt = nil
        phase = .paused
    }

    func resume() {
        guard phase == .paused else { return }
        if !engine.isRunning { try? engine.start() }
        pipe.paused = false
        resumedAt = Date()
        phase = .recording
    }

    /// Stop and return the finished transcript and the recorded length.
    func stop() async -> (lines: [TranscriptLine], duration: Double) {
        guard phase == .recording || phase == .paused else { return (lines, elapsed) }
        if phase == .recording { accumulated += Date().timeIntervalSince(resumedAt ?? Date()) }
        elapsed = accumulated
        phase = .finishing
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        pipe.finishInput()
        if let analyzer {
            try? await analyzer.finalizeAndFinishThroughEndOfInput()
        }
        await resultsTask?.value
        if !liveText.isEmpty {
            lines.append(TranscriptLine(t: lines.last?.end ?? 0, end: elapsed, text: liveText))
            liveText = ""
        }
        let result = (lines, elapsed)
        stopEverything()
        phase = .idle
        lectureID = nil
        return result
    }

    private func stopEverything() {
        clock?.cancel()
        clock = nil
        if engine.isRunning { engine.stop() }
        engine.inputNode.removeTap(onBus: 0)
        pipe.finishInput()
        analyzer = nil
        resultsTask = nil
        if let interruptionObserver { NotificationCenter.default.removeObserver(interruptionObserver) }
        interruptionObserver = nil
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    private func startClock() {
        clock = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .milliseconds(100))
                guard let self else { return }
                let running = self.resumedAt.map { Date().timeIntervalSince($0) } ?? 0
                self.elapsed = self.accumulated + running
                self.levels.removeFirst()
                self.levels.append(self.phase == .recording ? self.pipe.level : 0)
            }
        }
    }

    /// A phone call or Siri pauses recording; it resumes afterwards when iOS allows.
    private func observeInterruptions() {
        interruptionObserver = NotificationCenter.default.addObserver(
            forName: AVAudioSession.interruptionNotification, object: nil, queue: .main) { [weak self] note in
            let raw = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt
            let type = raw.flatMap(AVAudioSession.InterruptionType.init(rawValue:))
            let optionsRaw = note.userInfo?[AVAudioSessionInterruptionOptionKey] as? UInt ?? 0
            Task { @MainActor in
                guard let self else { return }
                if type == .began {
                    self.pause()
                    self.notice = "Paused by an interruption (a call or another app)."
                } else if type == .ended, AVAudioSession.InterruptionOptions(rawValue: optionsRaw).contains(.shouldResume) {
                    try? AVAudioSession.sharedInstance().setActive(true)
                    self.resume()
                    self.notice = nil
                }
            }
        }
    }
}

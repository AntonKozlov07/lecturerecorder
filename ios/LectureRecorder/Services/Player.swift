import AVFoundation
import Observation

/// Plays a lecture's recording from a given moment (tapping a timestamp).
@MainActor
@Observable
final class Player {
    private(set) var lectureID: String?
    private(set) var isPlaying = false
    private(set) var position: Double = 0
    private var player: AVAudioPlayer?
    private var ticker: Task<Void, Never>?

    func play(_ url: URL, lectureID: String, at seconds: Double) {
        do {
            if self.lectureID != lectureID || player == nil {
                try AVAudioSession.sharedInstance().setCategory(.playback, mode: .spokenAudio)
                try AVAudioSession.sharedInstance().setActive(true)
                player = try AVAudioPlayer(contentsOf: url)
                self.lectureID = lectureID
            }
            player?.currentTime = max(0, seconds)
            player?.play()
            isPlaying = true
            ticker?.cancel()
            ticker = Task { [weak self] in
                while !Task.isCancelled {
                    try? await Task.sleep(for: .milliseconds(250))
                    guard let self, let p = self.player else { return }
                    self.position = p.currentTime
                    if !p.isPlaying { self.isPlaying = false; return }
                }
            }
        } catch {
            isPlaying = false
        }
    }

    func toggle() {
        guard let player else { return }
        if player.isPlaying { player.pause(); isPlaying = false }
        else { player.play(); isPlaying = true }
    }

    func stop() {
        player?.stop()
        player = nil
        lectureID = nil
        isPlaying = false
        ticker?.cancel()
    }
}

# Lecture Recorder for iPhone

The iPhone app works on its own: it records (even with the screen locked),
transcribes on the phone, and writes notes, quizzes and flashcards with your
Anthropic API key. It syncs with the PC app through a private GitHub
repository.

You need an iPhone with **iOS 26 or later**, a Windows PC, and a free Apple ID.

## 1. Install it with AltStore (one time, about 15 minutes)

The app isn't on the App Store, so you install it yourself with
[AltStore](https://altstore.io), which signs it with your own Apple ID. Apps
installed this way expire after 7 days unless they're refreshed. AltStore does
that automatically in the background when your iPhone and PC are on the same
Wi-Fi and AltServer is running on the PC.

**On the PC**

1. Install **iTunes** and **iCloud** from Apple's website (the versions from
   apple.com, *not* the Microsoft Store versions). AltServer needs them.
2. Download **AltServer for Windows** from [altstore.io](https://altstore.io)
   and install it. It appears as a diamond icon in the system tray.
3. Connect your iPhone with a cable, unlock it, and tap **Trust** if asked.
   In iTunes, select the phone and tick **Sync with this iPhone over Wi-Fi**
   (this lets AltStore refresh apps without a cable later).

**Install AltStore on the iPhone**

4. Click the AltServer tray icon, choose **Install AltStore**, and pick your
   iPhone. Sign in with your Apple ID when asked.
5. On the iPhone, open **Settings > General > VPN & Device Management**, tap
   your Apple ID and tap **Trust**.
6. Turn on **Developer Mode**: **Settings > Privacy & Security > Developer
   Mode**, then restart when asked and confirm.

**Install Lecture Recorder**

7. On the iPhone, open the [latest build](https://github.com/AntonKozlov07/lecturerecorder/releases/tag/latest)
   in Safari and download **LectureRecorder-iPhone.ipa**.
8. Open **AltStore > My Apps**, tap **+**, and choose the downloaded file
   (in Files > Downloads). Keep AltServer running on the PC while it installs.

To update later, download the new `.ipa` the same way and install it over the
old one. Your lectures are kept.

**If it stops opening** after a week, the 7-day signature ran out: open
AltStore with the PC on the same Wi-Fi and tap **Refresh All**.

## 2. Set up sync (one time, about 3 minutes)

Lectures, notes, chats, quizzes, flashcards and course files sync through a
private GitHub repository that only you can see. Audio stays on the device that
recorded it.

1. Sign in at [github.com](https://github.com), click **New repository**, name
   it (for example `lecture-sync`), choose **Private**, and create it.
2. Open **Settings > Developer settings > Personal access tokens >
   Fine-grained tokens** and click **Generate new token**.
3. Under **Repository access**, choose **Only select repositories** and pick the
   new repository.
4. Under **Permissions > Repository permissions**, set **Contents** to **Read
   and write**. Generate the token and copy it.
5. On the PC, click **Sync** at the bottom of the sidebar, and paste the
   repository name (`your-username/lecture-sync`) and the token.
6. On the iPhone, open **Settings > Sync with your PC**, paste the same two
   values and tap **Save and sync now**.

Both apps then sync every couple of minutes, and the iPhone also syncs when you
open it and after each recording.

## Things to know

- **Transcription** uses Apple's on-device speech engine. The first recording in
  a language downloads its speech model (a one-time download handled by iOS).
- **Recording** continues with the screen locked or while you use other apps. A
  phone call pauses it; it resumes afterwards.
- **Course files**: on the iPhone you can add PDFs, text files and photos of
  handouts or whiteboards. Word and PowerPoint files can be added on the PC; their
  text syncs to the phone.
- **The old phone setup** from version 0.2/0.3 (the QR code and certificate) is
  gone. If you installed its certificate, remove it under **Settings > General >
  VPN & Device Management** (it's called "Lecture Recorder").

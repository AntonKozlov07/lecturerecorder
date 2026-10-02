# Sync format

The PC app and the iPhone app sync through a private GitHub repository that the
user owns. Each device reads and writes plain JSON files there through the GitHub
REST API. This file is the contract both implementations follow
(`lecturerecorder/sync.py` and `ios/LectureRecorder/Sync/`).

## Repository layout

```
lecture-recorder.json                    {"format": 1}: marks the repo as a sync store
lectures/<lecture id>.json               one lecture
courses/<course id>.json                 one course, including its file list
files/<course id>/<material id>/<name>   original course files (PDFs, images...), up to 25 MB each
deleted.json                             tombstones (see below)
```

IDs are 12 lowercase hex characters, generated randomly on the device that
creates the item, so devices never collide. Audio is not synced; it stays on the
device that recorded it.

## Lecture document

```json
{
  "format": 1,
  "id": "3f9a0c1d2e4b",
  "title": "Week 3: Entropy",
  "course": "PHYS 201",
  "created_at": 1790000000.0,
  "modified_at": 1790003600.0,
  "duration": 3125.4,
  "transcript": [
    {"t": 0.0, "end": 4.2, "text": "Today we start entropy.", "side_talk": false}
  ],
  "side_talk_checked": true,
  "notes": "# Markdown notes",
  "topics": [{"title": "", "summary": "", "timestamp": "12:03"}],
  "deep_dives": [{"topic": "", "content": "", "created_at": 1790000100.0}],
  "chat": [{"role": "user", "content": "", "created_at": 1790000200.0}],
  "quizzes": [{
    "created_at": 1790000300.0, "kind": "choice", "difficulty": "medium",
    "questions": [], "answers": null, "grading": null, "score": null
  }],
  "flashcards": [{"front": "", "back": "", "source": ""}]
}
```

* Times are Unix seconds (floats). `t` and `end` are seconds from the start of the recording.
* `questions`, `answers` and `grading` use the same shapes as the AI features
  (multiple choice: `question, options, answer_index, explanation, source`;
  written: `question, model_answer, key_points, source`; grading:
  `verdict, feedback`).
* A lecture is only uploaded once its transcript is complete (not while recording
  or transcribing).

## Course document

```json
{
  "format": 1,
  "id": "a81c3e0f9b27",
  "name": "PHYS 201",
  "created_at": 1790000000.0,
  "modified_at": 1790003600.0,
  "excluded": ["lecture:3f9a0c1d2e4b", "material:5e1f0a2b3c4d"],
  "materials": [{
    "id": "5e1f0a2b3c4d", "filename": "Chapter 4.pdf", "kind": "pdf",
    "pages": 32, "size": 1048576, "created_at": 1790000000.0,
    "text": "[Page 1] ...",
    "file": "files/a81c3e0f9b27/5e1f0a2b3c4d/Chapter 4.pdf"
  }],
  "chat": [], "quizzes": [], "flashcards": []
}
```

* `kind` is one of `pdf`, `pdf_scan` (no text layer: the AI reads the file itself),
  `slides`, `document`, `text`, `image`.
* `text` is the extracted text, so a device never has to parse a format it can't
  read (the iPhone doesn't open PowerPoint files, for example).
* `file` is null when the original is over 25 MB; for `pdf_scan` and `image` the
  AI needs the original, so such files over 25 MB are not synced at all.
* Lectures belong to a course by name (`lecture.course == course.name`).

## Tombstones

```json
{"lectures": {"<id>": 1790000000.0}, "courses": {}, "materials": {}}
```

When a device deletes an item it adds the ID with the deletion time. Devices
merge tombstones by union, delete matching local items, and never upload a
tombstoned item again. Material tombstones also remove the file from `files/`.

## Sync algorithm

Each device keeps, per path, the blob SHA it last saw on GitHub and a hash of its
own last uploaded or imported version.

1. Read the branch head, its tree (recursive) and `deleted.json`.
2. For every lecture and course:
   * only the local copy changed: upload it, with `modified_at` set to now;
   * only the GitHub copy changed: download and import it;
   * both changed: merge, import the result and upload it;
   * neither: nothing to do.
3. **Merging** two versions of a document:
   * `chat`, `deep_dives` and `quizzes` are combined, keyed by `created_at`
     rounded to milliseconds (plus `role` for chat, `topic` for deep dives).
     For a quiz present on both sides, the answered version wins; if both are
     answered, the one from the newer document wins.
   * `materials` are combined by `id`.
   * Everything else comes from the document with the newer `modified_at`.
4. Upload everything in a single commit using the Git Data API (blobs, tree,
   commit, then a non-forced ref update). If the ref update is rejected because
   the other device pushed first, start again from step 1.
5. An empty repository is initialised by creating `lecture-recorder.json` with
   the Contents API, since the Git Data API needs at least one commit.

## Courses with the same name

If a device downloads a course whose ID it doesn't know but it already has a
course with the same name that has never been synced, it adopts the downloaded
ID for its local course and merges the two. This happens when the same course
was created on both devices before their first sync.

## Course colors

Both apps color a course the same way, without storing the color:

1. A course's preferred color is `hash(id) % 8`, where `hash` is 32-bit FNV-1a over the id's UTF-16 code
   units followed by the MurmurHash3 finalizer (`h ^= h >> 16; h *= 0x85ebca6b; h ^= h >> 13;
   h *= 0xc2b2ae35; h ^= h >> 16`).
2. Courses are handed colors oldest first (`created_at`, then `id`). A course whose preferred color is taken
   gets the next free one, wrapping around, so up to 8 courses never share a color.

Sync keeps `created_at` identical on both devices, so the order, and the colors, match.

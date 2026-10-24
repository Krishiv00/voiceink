# VoiceInk

VoiceInk reads your writing back to you, out loud, as you write it.

Paste a draft, drop in a file, or point it at one on startup, and VoiceInk turns it into spoken narration, complete with spoken punctuation ("comma," "full stop"), structural cues ("new paragraph," "heading level two"), and a word-level highlight that follows along as it plays. The idea is simple: your ear catches things your eye skims past. Instead of constantly re-reading your own work from the top, you let it play while you keep writing, and listen for the sentence that doesn't sound right.

It's a small local desktop app, a Python/Flask backend wrapped in a native window via `pywebview`, using Microsoft Edge's neural text-to-speech voices for narration.

## Why

Writing is easy to proofread badly, because your eyes already know what you meant to say. Reading it out loud fixes that: awkward phrasing, run-ons, and missing words jump out immediately when you *hear* them. VoiceInk automates that read-aloud pass so you don't have to stop writing to do it yourself.

## Features

- **Live narration with punctuation spoken aloud**, not just the words, but the structure: commas, full stops, quotes, parentheses, and more, each read as a short spoken cue so you can actually follow the shape of a sentence by ear.
- **Structural cues**: paragraph breaks, headings (with level), list items, and quotes are announced before they're read, so you know where you are in the document without looking.
- **Word-level sync**: the text on screen highlights in step with the narration, so you can follow along visually if you want to.
- **Scrub, skip, and seek**: jump forward or back by sentence-sized unit, scrub through the whole document on a slider, or restart from the top, all without losing your place.
- **Broad document support**: supports a range of common file types, parsed into proper structured blocks (headings, paragraphs, lists, quotes) rather than flattened into one wall of text.
- **Adjustable pacing**: control both the writing-style pause length between clusters (chars/minute) and the raw narration speed of the voice itself, independently.
- **Custom vocabulary**: rename or mute individual punctuation marks, and rewrite the wording of structural cues, so the narration sounds the way you want it to.
- **Background prefetching**: narration for upcoming text generates ahead of playback in the background, so there's no stutter waiting on audio mid-read.
- **Drag-and-drop loading**: drop a file to load it.

## How it works

Under the hood, VoiceInk breaks your document into small "narration units," short clusters of words sized for natural-sounding pauses, roughly sentence-fragment length, rather than reading word by word or paragraph by paragraph. Each unit is synthesized through `edge-tts`, batched into small audio chunks so the app never has to wait long for the next bit of narration, and chunks ahead of your current position are generated in the background while you listen to the current one.

## Requirements

- Python 3.10 or newer
- [FFmpeg](https://ffmpeg.org/download.html) installed and available on your system's `PATH`. This is required for audio playback to work at all; the app will fail to produce sound without it.
- On Python 3.13 or newer specifically, also install `audioop-lts` (a module one of the audio dependencies needs that was removed from Python's standard library in 3.13). Not needed on Python 3.12 or earlier.

Install all Python dependencies with:

```
pip install flask pywebview edge-tts pydub python-docx pypdf
```

If you're on Python 3.13 or newer, also run:

```
pip install audioop-lts
```

## Getting started

1. Install the requirements above.
2. Run `main.py` to launch the app in its own window, or run `app.py` directly to start it as a local web server and open it in your browser automatically.
3. Paste some text, or drop or choose a file, and hit play.

Runs on Windows, macOS, and Linux.

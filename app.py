import io
import json
import os
import sys
import traceback
from flask import Flask, request, jsonify, render_template, send_file, session as flask_session
import uuid

from extractors import extract_file, extract_plain_text, EXTRACTORS
from narration_prep import (
    build_narration_units,
    apply_punctuation_overrides,
    apply_structure_cue_overrides,
    CUSTOMIZABLE_PUNCT_KEYS,
    PUNCT_NAMES,
    STRUCTURE_CUES,
    HEADING_LEVEL_CUE_TEMPLATE,
)
from tts_engine import NarrationSession, UNITS_PER_CHUNK, AVAILABLE_VOICES, VOICE_IDS, DEFAULT_VOICE

app = Flask(__name__)
app.secret_key = "voiceink-local-secret"

SESSIONS: dict[str, NarrationSession] = {}

DEFAULT_CHARS_PER_MINUTE = 150.0
DEFAULT_SPEECH_RATE_PCT = 0

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")


def load_settings() -> dict:
    defaults = {
        "chars_per_minute": DEFAULT_CHARS_PER_MINUTE,
        "speech_rate_pct": DEFAULT_SPEECH_RATE_PCT,
        "voice": DEFAULT_VOICE,
    }
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return defaults
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        print(f"[settings] couldn't read {SETTINGS_PATH} ({type(e).__name__}: {e}); using defaults")
        return defaults

    if not isinstance(data, dict):
        return defaults
    defaults["chars_per_minute"] = _coerce_float(data.get("chars_per_minute"), DEFAULT_CHARS_PER_MINUTE)
    defaults["speech_rate_pct"] = _coerce_int(data.get("speech_rate_pct"), DEFAULT_SPEECH_RATE_PCT)

    saved_voice = data.get("voice")
    defaults["voice"] = saved_voice if saved_voice in VOICE_IDS else DEFAULT_VOICE
    return defaults


def save_settings(chars_per_minute: float, speech_rate_pct: int, voice: str) -> None:
    _write_settings_file({
        "chars_per_minute": chars_per_minute,
        "speech_rate_pct": speech_rate_pct,
        "voice": voice,
    })


def _write_settings_file(payload: dict) -> None:
    try:
        tmp_path = SETTINGS_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp_path, SETTINGS_PATH)
    except OSError as e:
        print(f"[settings] couldn't save {SETTINGS_PATH} ({type(e).__name__}: {e})")


VOCABULARY_PATH = os.path.join(APP_DIR, "vocabulary.json")


def load_vocabulary() -> dict:
    defaults = {"punct_overrides": {}, "cue_overrides": {}, "heading_level_cue_template": None}
    try:
        with open(VOCABULARY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return defaults
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        print(f"[vocabulary] couldn't read {VOCABULARY_PATH} ({type(e).__name__}: {e}); using defaults")
        return defaults

    if not isinstance(data, dict):
        return defaults

    punct_overrides = data.get("punct_overrides")
    if isinstance(punct_overrides, dict):
        defaults["punct_overrides"] = {
            k: v for k, v in punct_overrides.items()
            if k in CUSTOMIZABLE_PUNCT_KEYS and isinstance(v, str)
        }

    cue_overrides = data.get("cue_overrides")
    if isinstance(cue_overrides, dict):
        defaults["cue_overrides"] = {
            k: v for k, v in cue_overrides.items()
            if k in STRUCTURE_CUES and isinstance(v, str) and v.strip()
        }

    template = data.get("heading_level_cue_template")
    if isinstance(template, str) and "{level}" in template:
        defaults["heading_level_cue_template"] = template

    return defaults


def save_vocabulary(punct_overrides: dict, cue_overrides: dict, heading_level_cue_template: str | None) -> None:
    try:
        tmp_path = VOCABULARY_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({
                "punct_overrides": punct_overrides,
                "cue_overrides": cue_overrides,
                "heading_level_cue_template": heading_level_cue_template,
            }, f)
        os.replace(tmp_path, VOCABULARY_PATH)
    except OSError as e:
        print(f"[vocabulary] couldn't save {VOCABULARY_PATH} ({type(e).__name__}: {e})")


def build_vocabulary_payload() -> dict:
    saved = SAVED_VOCABULARY
    return {
        "punctuation": [
            {
                "symbol": key,
                "default_name": PUNCT_NAMES[key],
                "name": saved["punct_overrides"].get(key, PUNCT_NAMES[key]),
                "muted": saved["punct_overrides"].get(key, PUNCT_NAMES[key]) == "",
            }
            for key in CUSTOMIZABLE_PUNCT_KEYS
        ],
        "cues": [
            {
                "key": key,
                "default_text": STRUCTURE_CUES[key],
                "text": saved["cue_overrides"].get(key, STRUCTURE_CUES[key]),
            }
            for key in STRUCTURE_CUES
        ],
        "heading_level_cue_template": {
            "default": HEADING_LEVEL_CUE_TEMPLATE,
            "value": saved["heading_level_cue_template"] or HEADING_LEVEL_CUE_TEMPLATE,
        },
    }


def _coerce_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


SAVED_SETTINGS = load_settings()
SAVED_VOCABULARY = load_vocabulary()


@app.route("/")
def index():
    return render_template(
        "index.html",
        supported_extensions=sorted(EXTRACTORS.keys()),
        saved_chars_per_minute=SAVED_SETTINGS["chars_per_minute"],
        saved_speech_rate_pct=SAVED_SETTINGS["speech_rate_pct"],
        available_voices=AVAILABLE_VOICES,
        saved_voice=SAVED_SETTINGS["voice"],
        vocabulary=build_vocabulary_payload(),
    )


@app.route("/api/load/file", methods=["POST"])
def load_file():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400

    file_bytes = f.read()
    try:
        blocks = extract_file(f.filename, file_bytes)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Failed to read file: {e}"}), 400

    return _start_session(blocks, source_name=f.filename)


@app.route("/api/load/text", methods=["POST"])
def load_text():
    data = request.get_json(force=True, silent=True) or {}
    text = data.get("text", "")
    if not text.strip():
        return jsonify({"error": "No text provided"}), 400

    blocks = extract_plain_text(text)
    return _start_session(blocks, source_name="Pasted text")


def _start_session(blocks, source_name: str):
    if not blocks:
        return jsonify({"error": "No readable text found in the input"}), 400

    units = build_narration_units(
        blocks,
        punct_names=apply_punctuation_overrides(SAVED_VOCABULARY["punct_overrides"]),
        structure_cues=apply_structure_cue_overrides(SAVED_VOCABULARY["cue_overrides"]),
        heading_level_cue_template=SAVED_VOCABULARY["heading_level_cue_template"],
    )
    if not any(u.kind == "cluster" for u in units):
        return jsonify({"error": "No narratable words found in the input"}), 400

    block_meta = {
        b_index: {"type": b["type"], "level": b.get("level")}
        for b_index, b in enumerate(blocks)
    }

    old_sid = flask_session.pop("sid", None)
    SESSIONS.pop(old_sid, None)

    sid = str(uuid.uuid4())
    narration_session = NarrationSession(
        units,
        chars_per_minute=SAVED_SETTINGS["chars_per_minute"],
        speech_rate_pct=SAVED_SETTINGS["speech_rate_pct"],
        voice=SAVED_SETTINGS["voice"],
    )
    SESSIONS[sid] = narration_session
    flask_session["sid"] = sid

    unit_views = [
        {
            "display_text": u.display_text,
            "kind": u.kind,
            "block_index": u.block_index,
            "is_new_block": u.is_new_block,
            "block_type": block_meta.get(u.block_index, {}).get("type"),
            "block_level": block_meta.get(u.block_index, {}).get("level"),
        }
        for u in units
    ]

    return jsonify({
        "session_id": sid,
        "source_name": source_name,
        "total_chunks": narration_session.total_chunks,
        "units_per_chunk": UNITS_PER_CHUNK,
        "units": unit_views,
    })


@app.route("/api/chunk/<int:index>")
def get_chunk(index: int):
    sid = flask_session.get("sid")
    narration_session = SESSIONS.get(sid)
    if narration_session is None:
        return jsonify({"error": "No active session. Load a document first."}), 400

    try:
        chunk = narration_session.get_chunk(index)
    except IndexError:
        return jsonify({"error": "Chunk index out of range"}), 404
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Narration generation failed: {type(e).__name__}: {e}"}), 500

    return send_file(
        io.BytesIO(chunk.audio_bytes),
        mimetype="audio/mpeg",
        as_attachment=False,
        download_name=f"chunk_{index}.mp3",
    ), 200, {
        "X-Chunk-Index": str(index),
        "X-Chunk-Duration-Ms": str(chunk.duration_ms),
        "X-Total-Chunks": str(narration_session.total_chunks),
        "X-Unit-Starts": ",".join(str(ms) for ms in chunk.unit_starts_ms),
    }



@app.route("/api/config", methods=["POST"])
def update_config():
    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify({"error": "Expected a JSON object"}), 400

    new_cpm = SAVED_SETTINGS["chars_per_minute"]
    new_rate = SAVED_SETTINGS["speech_rate_pct"]
    new_voice = SAVED_SETTINGS["voice"]

    if "chars_per_minute" in data:
        new_cpm = _coerce_float(
            data["chars_per_minute"],
            new_cpm,
        )

    if "speech_rate_pct" in data:
        new_rate = _coerce_int(
            data["speech_rate_pct"],
            new_rate,
        )

    if "voice" in data:
        candidate_voice = data["voice"]
        if candidate_voice not in VOICE_IDS:
            return jsonify({
                "error": f"Unknown voice '{candidate_voice}'"
            }), 400
        new_voice = candidate_voice

    import math

    if not math.isfinite(new_cpm) or new_cpm <= 0:
        return jsonify({
            "error": "chars_per_minute must be a positive finite number"
        }), 400

    if not -100 <= new_rate <= 100:
        return jsonify({
            "error": "speech_rate_pct must be between -100 and 100"
        }), 400

    sid = flask_session.get("sid")
    narration_session = SESSIONS.get(sid)

    if narration_session is not None:
        narration_session.update_settings(
            chars_per_minute=new_cpm,
            speech_rate_pct=new_rate,
            voice=new_voice,
        )

    SAVED_SETTINGS["chars_per_minute"] = new_cpm
    SAVED_SETTINGS["speech_rate_pct"] = new_rate
    SAVED_SETTINGS["voice"] = new_voice
    save_settings(new_cpm, new_rate, new_voice)

    return jsonify({
        "ok": True,
        "chars_per_minute": new_cpm,
        "speech_rate_pct": new_rate,
        "voice": new_voice,
    })


@app.route("/api/vocabulary")
def get_vocabulary():
    return jsonify(build_vocabulary_payload())


@app.route("/api/vocabulary", methods=["POST"])
def update_vocabulary():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Expected a JSON object"}), 400

    punct_overrides = data.get("punct_overrides", {})
    if not isinstance(punct_overrides, dict):
        return jsonify({"error": "punct_overrides must be an object"}), 400
    clean_punct = {}
    for key, value in punct_overrides.items():
        if key not in CUSTOMIZABLE_PUNCT_KEYS:
            return jsonify({"error": f"'{key}' is not a customizable punctuation mark"}), 400
        if not isinstance(value, str):
            return jsonify({"error": f"Spoken name for '{key}' must be a string"}), 400
        clean_punct[key] = value

    cue_overrides = data.get("cue_overrides", {})
    if not isinstance(cue_overrides, dict):
        return jsonify({"error": "cue_overrides must be an object"}), 400
    clean_cues = {}
    for key, value in cue_overrides.items():
        if key not in STRUCTURE_CUES:
            return jsonify({"error": f"'{key}' is not a known structure cue"}), 400
        if not isinstance(value, str) or not value.strip():
            return jsonify({"error": f"Cue text for '{key}' must be a non-empty string"}), 400
        clean_cues[key] = value

    template = data.get("heading_level_cue_template")
    clean_template = None
    if template is not None:
        if not isinstance(template, str) or "{level}" not in template:
            return jsonify({
                "error": "heading_level_cue_template must be a string containing {level}"
            }), 400
        clean_template = template

    SAVED_VOCABULARY["punct_overrides"] = clean_punct
    SAVED_VOCABULARY["cue_overrides"] = clean_cues
    SAVED_VOCABULARY["heading_level_cue_template"] = clean_template
    save_vocabulary(clean_punct, clean_cues, clean_template)

    return jsonify({"ok": True, **build_vocabulary_payload()})



@app.route("/api/new", methods=["POST"])
def new_document():
    sid = flask_session.pop("sid", None)
    SESSIONS.pop(sid, None)
    return jsonify({"ok": True})


@app.route("/api/prefetch/<int:index>", methods=["POST"])
def prefetch(index: int):
    sid = flask_session.get("sid")
    narration_session = SESSIONS.get(sid)
    if narration_session is None:
        return jsonify({"error": "No active session"}), 400
    narration_session.prefetch_async(index)
    return jsonify({"ok": True})


STARTUP_FILE_PATH: str | None = None


@app.route("/api/startup-file")
def get_startup_file():
    global STARTUP_FILE_PATH
    path = STARTUP_FILE_PATH
    STARTUP_FILE_PATH = None
    if not path or not os.path.isfile(path):
        return jsonify({"path": None})
    return jsonify({"path": path, "name": os.path.basename(path)})


@app.route("/api/load/path", methods=["POST"])
def load_path():
    data = request.get_json(force=True, silent=True) or {}
    path = data.get("path")
    if not isinstance(path, str) or not path:
        return jsonify({"error": "No path provided"}), 400
    if not os.path.isfile(path):
        return jsonify({"error": "File not found"}), 404

    name = os.path.basename(path)
    try:
        with open(path, "rb") as f:
            file_bytes = f.read()
        blocks = extract_file(name, file_bytes)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Failed to read file: {e}"}), 400

    return _start_session(blocks, source_name=name)


def main(startup_file: str | None = None):
    global STARTUP_FILE_PATH
    STARTUP_FILE_PATH = startup_file

    app.run(debug=False, port=5151, use_reloader=False)


if __name__ == "__main__":
    import threading
    import webbrowser

    threading.Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:5151")).start()

    main(sys.argv[1] if len(sys.argv) > 1 else None)

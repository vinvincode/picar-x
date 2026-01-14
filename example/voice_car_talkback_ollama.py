#!/usr/bin/env python3

import os
import time
import re

from picarx import Picarx
from picarx.stt import Vosk
from picarx.tts import Piper
from picarx.llm import Ollama

# systemd services often have no login session; picarx uses os.getlogin()
# which can crash with OSError -25. Force a stable username.
try:
    os.getlogin()
except Exception:
    os.getlogin = lambda: "picar"
    os.environ.setdefault("LOGNAME", "picar")
    os.environ.setdefault("USER", "picar")
    os.environ.setdefault("HOME", "/home/picar")

# ----------------------------
# Modes
# ----------------------------
MODE_DRIVE = "DRIVE"
MODE_AI = "AI"

# ----------------------------
# Setup
# ----------------------------
px = Picarx()
stt = Vosk(language="en-us")

tts = Piper()
tts.set_model("en_US-ryan-low")

INSTRUCTIONS = (
    "You are a helpful assistant. "
    "Output must be plain text only. "
    "Do not use markdown or formatting characters. "
    "Keep answers short (1 to 3 sentences)."
)

WELCOME = (
    "Hello. Say hey wally. Say start for drive mode, ai mode for questions, "
    "or safe mode for autonomous driving and obstacle avoidance."
)

llm = Ollama(ip="localhost", model="llama3.2:3b")
llm.set_max_messages(20)
llm.set_instructions(INSTRUCTIONS)
llm.set_welcome(WELCOME)

WAKE_WORDS = ["hey wally"]

mode = MODE_DRIVE
drive_enabled = False

# DRIVE settings
speed = 30          # 10..60
STEER_LEFT = -25
STEER_RIGHT = 25
STEER_CENTER = 0

PULSE_TIME = 1.0
drive_style = "pulse"  # pulse / continuous
continuous_motion = "STOP"  # STOP / FWD / BACK

# Circle
circle_active = False
CIRCLE_STEER = 25

# Servo feedback
SERVO_CENTER = 0
SERVO_LOOK_LEFT = -20
SERVO_LOOK_RIGHT = 20

# Streaming speech chunking
SAY_CHUNK_MIN_CHARS = 70
SAY_CHUNK_MAX_CHARS = 160
SAY_END_PUNCT = {".", "!", "?", "\n"}


# ----------------------------
# Helpers
# ----------------------------
def clamp(v, lo, hi):
    return max(lo, min(hi, v))

def clean_for_speech(text: str) -> str:
    t = text.replace("*", "").replace("#", "").replace("_", "")
    t = t.replace("`", "")
    t = re.sub(r"\s+", " ", t).strip()
    return t

def say(msg: str):
    msg = clean_for_speech((msg or "").strip())
    if not msg:
        return
    print("TTS:", msg)
    try:
        px.set_dir_servo_angle(SERVO_CENTER)
        tts.say(msg)
    except Exception as e:
        print("TTS error:", e)

def stop_car(center=True):
    global circle_active, continuous_motion
    circle_active = False
    continuous_motion = "STOP"
    px.stop()
    if center:
        px.set_dir_servo_angle(0)

def signal_listening():
    px.set_dir_servo_angle(SERVO_LOOK_LEFT)
    time.sleep(0.08)
    px.set_dir_servo_angle(SERVO_LOOK_RIGHT)
    time.sleep(0.08)
    px.set_dir_servo_angle(SERVO_CENTER)

def signal_thinking():
    px.set_dir_servo_angle(-28)
    time.sleep(0.14)
    px.set_dir_servo_angle(28)
    time.sleep(0.14)
    px.set_dir_servo_angle(-28)
    time.sleep(0.14)
    px.set_dir_servo_angle(SERVO_CENTER)

def should_say_chunk(buf: str) -> bool:
    if not buf:
        return False
    if len(buf) >= SAY_CHUNK_MAX_CHARS:
        return True
    if any(buf.endswith(p) for p in SAY_END_PUNCT):
        return len(buf) >= 25
    return len(buf) >= SAY_CHUNK_MIN_CHARS

def speak_streaming_response(token_stream):
    buf = ""
    spoken_any = False
    for token in token_stream:
        if not token:
            continue
        print(token, end="", flush=True)
        buf += token

        if not spoken_any and len(buf.strip()) >= 25:
            say(buf)
            buf = ""
            spoken_any = True
            continue

        if should_say_chunk(buf):
            say(buf)
            buf = ""

    print()
    if buf.strip():
        say(buf)

def ask_llm_and_speak(question: str):
    print("LLM question:", question)
    try:
        signal_thinking()
        response = llm.prompt(question, stream=True)
    except Exception as e:
        say(f"Sorry, I couldn't reach the local model. {e}")
        return
    speak_streaming_response(response)

def is_question(text: str) -> bool:
    t = text.strip().lower()
    if t.endswith("?"):
        return True
    starters = ("ask ", "question ", "what ", "when ", "why ", "how ", "who ", "where ", "which ", "can you ", "do you ")
    return t.startswith(starters)

def extract_number(text: str):
    m = re.search(r"(-?\d+)", text)
    return int(m.group(1)) if m else None

def sanitize_distance(raw):
    if raw is None:
        return None
    try:
        d = float(raw)
    except Exception:
        return None
    if d < 0:
        return None
    return round(d, 2)


# ----------------------------
# SAFE MODE (strong escape attempts)
# ----------------------------
def run_safe_mode():
    POWER = 45
    SafeDistance = 40.0
    DangerDistance = 20.0

    MAX_BLOCKED_ATTEMPTS = 14
    MAX_INVALID_READS = 20
    BACK_TIME = 0.70
    FORWARD_RECOVER_TIME = 0.35
    TURN_TIME = 0.18

    ESCAPE_LEFT_ANGLE = 35
    ESCAPE_RIGHT_ANGLE = -35

    say("Safe mode. Autonomous driving and obstacle avoidance.")
    print("SAFE MODE STARTED")

    blocked_attempts = 0
    invalid_reads = 0
    escape_side = "LEFT"

    try:
        while True:
            raw = px.ultrasonic.read()
            distance = sanitize_distance(raw)

            if distance is None:
                invalid_reads += 1
                px.stop()
                time.sleep(0.12)
                if invalid_reads >= MAX_INVALID_READS:
                    say("Safe mode stopped. Ultrasonic sensor not responding.")
                    break
                continue

            invalid_reads = 0
            print("distance:", distance)

            if distance >= SafeDistance:
                blocked_attempts = 0
                px.set_dir_servo_angle(0)
                px.forward(POWER)
                time.sleep(0.02)
                continue

            if distance >= DangerDistance:
                blocked_attempts = 0
                px.set_dir_servo_angle(30)
                px.forward(POWER)
                time.sleep(TURN_TIME)
                continue

            # Too close: escape routine
            blocked_attempts += 1

            px.set_dir_servo_angle(-30)
            px.backward(POWER)
            time.sleep(BACK_TIME)
            px.stop()
            time.sleep(0.08)

            if escape_side == "LEFT":
                px.set_dir_servo_angle(ESCAPE_LEFT_ANGLE)
                escape_side = "RIGHT"
            else:
                px.set_dir_servo_angle(ESCAPE_RIGHT_ANGLE)
                escape_side = "LEFT"

            px.forward(POWER)
            time.sleep(FORWARD_RECOVER_TIME)
            px.stop()
            time.sleep(0.08)

            if blocked_attempts >= MAX_BLOCKED_ATTEMPTS:
                px.stop()
                px.set_dir_servo_angle(0)
                say("Safe mode stopped. I could not find a safe path.")
                break

    finally:
        px.stop()
        px.set_dir_servo_angle(0)
        print("SAFE MODE ENDED")


# ----------------------------
# DRIVE helpers
# ----------------------------
def drive_forward_pulse():
    px.set_dir_servo_angle(STEER_CENTER)
    px.forward(speed)
    time.sleep(PULSE_TIME)
    px.stop()

def drive_backward_pulse():
    px.set_dir_servo_angle(STEER_CENTER)
    px.backward(speed)
    time.sleep(PULSE_TIME)
    px.stop()

def drive_left_pulse():
    px.set_dir_servo_angle(STEER_LEFT)
    px.forward(speed)
    time.sleep(PULSE_TIME)
    px.stop()
    px.set_dir_servo_angle(STEER_CENTER)

def drive_right_pulse():
    px.set_dir_servo_angle(STEER_RIGHT)
    px.forward(speed)
    time.sleep(PULSE_TIME)
    px.stop()
    px.set_dir_servo_angle(STEER_CENTER)

def start_circle():
    global circle_active, continuous_motion, drive_style
    circle_active = True
    drive_style = "continuous"
    continuous_motion = "FWD"
    px.set_dir_servo_angle(CIRCLE_STEER)
    px.forward(speed)

def refresh_continuous():
    if not drive_enabled:
        return
    if circle_active:
        px.set_dir_servo_angle(CIRCLE_STEER)
        px.forward(speed)
        return
    if continuous_motion == "FWD":
        px.set_dir_servo_angle(STEER_CENTER)
        px.forward(speed)
    elif continuous_motion == "BACK":
        px.set_dir_servo_angle(STEER_CENTER)
        px.backward(speed)


# ----------------------------
# Main
# ----------------------------
say(WELCOME)
print(WELCOME)
print('Say "hey wally" to wake. Say "sleep" to pause. Ctrl+C to quit.')

try:
    while True:
        stt.wait_until_heard(WAKE_WORDS)
        say("Ready.")
        print("Wake word detected. Listening... (say 'sleep' to pause)")

        while True:
            # Keep continuous motion going
            if mode == MODE_DRIVE and drive_style == "continuous":
                refresh_continuous()

            if mode == MODE_AI:
                signal_listening()

            res = stt.listen(stream=False)
            text = res.get("text", "") if isinstance(res, dict) else str(res)
            text = (text or "").lower().strip()
            if not text:
                continue

            print("Heard:", text)

            # --- wake loop control ---
            if "sleep" in text:
                stop_car(center=True)
                drive_enabled = False
                mode = MODE_DRIVE
                say("Sleeping.")
                break

            # --- mode switches (accept common mishears) ---
            if "ai mode" in text or "a mod" in text or "a more" in text:
                mode = MODE_AI
                drive_enabled = False
                stop_car(center=True)
                say("AI mode. Ask me a question.")
                continue

            if "drive mode" in text or "dr more" in text or "drive" == text:
                mode = MODE_DRIVE
                drive_enabled = False
                stop_car(center=True)
                say("Drive mode.")
                continue

            if "safe mode" in text or "save mode" in text or "save more" in text or "object avoidance" in text:
                stop_car(center=True)
                drive_enabled = False
                mode = MODE_DRIVE
                run_safe_mode()
                say(WELCOME)
                print(WELCOME)
                continue

            # --- stop/start ---
            if "stop" in text or "disable" in text:
                drive_enabled = False
                stop_car(center=True)
                say("Stopped.")
                continue

            if "start" in text or "enable" in text:
                if mode == MODE_AI:
                    say("You are in AI mode. Say drive mode to drive.")
                else:
                    drive_enabled = True
                    say("Drive enabled.")
                continue

            # --- AI mode ---
            if mode == MODE_AI:
                stop_car(center=False)
                ask_llm_and_speak(text)
                continue

            # --- DRIVE mode commands ---
            if is_question(text):
                say("Say AI mode if you want me to answer questions.")
                continue

            if not drive_enabled:
                say("Say start first.")
                continue

            # speed
            if "faster" in text:
                speed = clamp(speed + 5, 10, 60)
                say(f"Speed {speed}.")
                continue

            if "slower" in text:
                speed = clamp(speed - 5, 10, 60)
                say(f"Speed {speed}.")
                continue

            if "speed" in text:
                n = extract_number(text)
                if n is not None:
                    speed = clamp(n, 10, 60)
                    say(f"Speed set to {speed}.")
                continue

            # driving style
            if "pulse mode" in text:
                drive_style = "pulse"
                circle_active = False
                continuous_motion = "STOP"
                stop_car(center=True)
                say("Pulse mode.")
                continue

            if "continuous mode" in text:
                drive_style = "continuous"
                say("Continuous mode.")
                continue

            # motion
            if "forward" in text:
                circle_active = False
                if drive_style == "pulse":
                    drive_forward_pulse()
                else:
                    continuous_motion = "FWD"
                say("Forward.")
                continue

            if "backward" in text or "backwards" in text or "reverse" in text:
                circle_active = False
                if drive_style == "pulse":
                    drive_backward_pulse()
                else:
                    continuous_motion = "BACK"
                say("Backward.")
                continue

            if text == "left":
                circle_active = False
                if drive_style == "pulse":
                    drive_left_pulse()
                else:
                    px.set_dir_servo_angle(STEER_LEFT)
                    px.forward(speed)
                    continuous_motion = "FWD"
                say("Left.")
                continue

            if text == "right":
                circle_active = False
                if drive_style == "pulse":
                    drive_right_pulse()
                else:
                    px.set_dir_servo_angle(STEER_RIGHT)
                    px.forward(speed)
                    continuous_motion = "FWD"
                say("Right.")
                continue

            if "straight" in text or "center" in text:
                circle_active = False
                px.set_dir_servo_angle(STEER_CENTER)
                say("Straight.")
                continue

            if "circle" in text:
                start_circle()
                say("Circling.")
                continue

            say("Command not recognized.")

except KeyboardInterrupt:
    pass
finally:
    stop_car(center=True)
    try:
        say("Goodbye.")
    except Exception:
        pass
    print("Stopped and centered. Bye.")

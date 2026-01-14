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

# State
mode = MODE_DRIVE
drive_enabled = False

# DRIVE settings
speed = 30          # 10..60
STEER_LEFT = -25
STEER_RIGHT = 25
STEER_CENTER = 0

PULSE_TIME = 1.0
drive_style = "pulse"       # "pulse" or "continuous"
continuous_motion = "STOP"  # "STOP" / "FWD" / "BACK"
circle_active = False
CIRCLE_STEER = 25

# Servo feedback
SERVO_CENTER = 0
SERVO_LOOK_LEFT = -20
SERVO_LOOK_RIGHT = 20

# Camera pan/tilt angles (keep state so we can do fun motions)
cam_pan = 0
cam_tilt = 0
CAM_MIN = -35
CAM_MAX = 35

# Speech chunking
SAY_CHUNK_MIN_CHARS = 70
SAY_CHUNK_MAX_CHARS = 160
SAY_END_PUNCT = {".", "!", "?", "\n"}

# Safe mode trigger aliases
SAFE_MODE_TRIGGERS = ("safe mode", "save mode", "save more", "object avoidance")

# AI/Drive mishears
AI_MODE_TRIGGERS = ("ai mode", "a mod", "a more", "a mode")
DRIVE_MODE_TRIGGERS = ("drive mode", "dr more", "dr mode", "drive")

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
        camera_nod(3)
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
# Camera control (pan/tilt)
# Uses px.set_cam_pan_angle() and px.set_cam_tilt_angle() per docs. :contentReference[oaicite:1]{index=1}
# ----------------------------
def camera_supported() -> bool:
    return hasattr(px, "set_cam_pan_angle") and hasattr(px, "set_cam_tilt_angle")

def set_cam_pan(angle: int):
    global cam_pan
    cam_pan = int(clamp(angle, CAM_MIN, CAM_MAX))
    if not camera_supported():
        raise RuntimeError("Camera pan/tilt servos not available.")
    px.set_cam_pan_angle(cam_pan)

def set_cam_tilt(angle: int):
    global cam_tilt
    cam_tilt = int(clamp(angle, CAM_MIN, CAM_MAX))
    if not camera_supported():
        raise RuntimeError("Camera pan/tilt servos not available.")
    px.set_cam_tilt_angle(cam_tilt)

def camera_center():
    set_cam_pan(0)
    set_cam_tilt(0)

def camera_nod(times=2):
    """Nod = tilt up/down a few times."""
    start = cam_tilt
    for _ in range(times):
        set_cam_tilt(clamp(start - 18, CAM_MIN, CAM_MAX))
        time.sleep(0.18)
        set_cam_tilt(clamp(start + 10, CAM_MIN, CAM_MAX))
        time.sleep(0.18)
    set_cam_tilt(start)

def camera_shake(times=2):
    """Shake head = pan left/right."""
    start = cam_pan
    for _ in range(times):
        set_cam_pan(clamp(start - 20, CAM_MIN, CAM_MAX))
        time.sleep(0.18)
        set_cam_pan(clamp(start + 20, CAM_MIN, CAM_MAX))
        time.sleep(0.18)
    set_cam_pan(start)

def camera_scan():
    """Scan slowly left to right."""
    start_tilt = cam_tilt
    set_cam_tilt(start_tilt)  # hold tilt
    for a in [-30, -15, 0, 15, 30, 0]:
        set_cam_pan(a)
        time.sleep(0.25)

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
# Fun drive-mode actions
# ----------------------------
def fun_dance():
    """A quick wiggle dance (steering + small motor bursts)."""
    px.stop()
    for ang in [-25, 25, -25, 25, 0]:
        px.set_dir_servo_angle(ang)
        px.forward(25)
        time.sleep(0.20)
        px.stop()
        time.sleep(0.08)
    px.set_dir_servo_angle(0)

def fun_spin_short():
    """Not a true spin (no differential), but a tight circle burst."""
    px.set_dir_servo_angle(35)
    px.forward(35)
    time.sleep(1.0)
    px.stop()
    px.set_dir_servo_angle(0)

def fun_lookaround():
    """Camera scan + head shake if camera exists; otherwise steering wiggle."""
    if camera_supported():
        camera_scan()
        camera_nod(1)
        camera_shake(1)
        camera_center()
    else:
        for ang in [-20, 20, 0]:
            px.set_dir_servo_angle(ang)
            time.sleep(0.2)

# ----------------------------
# SAFE MODE (strong escape attempts)
# ----------------------------
def run_safe_mode():
    """
    Autonomous obstacle avoidance loop.
    Tries hard to escape by backing up and alternating turns.
    """
    POWER = 45
    SafeDistance = 40.0
    DangerDistance = 20.0

    MAX_BLOCKED_ATTEMPTS = 16
    MAX_INVALID_READS = 30
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
# Help text (Drive mode)
# ----------------------------
def help_drive():
    return (
        "Drive commands: start, stop, forward, backward, left, right, straight, circle. "
        "Speed commands: faster, slower, speed 40. "
        "Modes: pulse mode, continuous mode. "
        "Fun: dance, spin, look around, nod, shake head, camera center, camera left, camera right, camera up, camera down."
    )

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
                camera_nod(3)

            res = stt.listen(stream=False)
            text = res.get("text", "") if isinstance(res, dict) else str(res)
            text = (text or "").lower().strip()
            if not text:
                continue

            print("Heard:", text)

            # --- session control ---
            if "sleep" in text:
                stop_car(center=True)
                drive_enabled = False
                mode = MODE_DRIVE
                say("Sleeping.")
                break

            if "help" in text:
                say(help_drive())
                continue

            # --- mode switches (accept mishears) ---
            if any(k in text for k in AI_MODE_TRIGGERS):
                mode = MODE_AI
                drive_enabled = False
                stop_car(center=True)
                say("AI mode. Ask me a question.")
                continue

            if any(k in text for k in DRIVE_MODE_TRIGGERS):
                mode = MODE_DRIVE
                drive_enabled = False
                stop_car(center=True)
                say("Drive mode.")
                continue

            if any(k in text for k in SAFE_MODE_TRIGGERS):
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

            # --- Drive mode only below ---
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

            # fun commands
            if "dance" in text:
                say("Dancing.")
                fun_dance()
                continue

            if "spin" in text:
                say("Spinning.")
                fun_spin_short()
                continue

            if "look around" in text or "lookaround" in text:
                say("Looking around.")
                fun_lookaround()
                continue

            # camera fun
            if "nod" in text:
                if camera_supported():
                    say("Nodding.")
                    camera_nod(2)
                else:
                    say("Camera servos are not connected.")
                continue

            if "shake" in text or "shake head" in text:
                if camera_supported():
                    say("Shaking head.")
                    camera_shake(2)
                else:
                    say("Camera servos are not connected.")
                continue

            if "camera center" in text or "center camera" in text:
                if camera_supported():
                    camera_center()
                    say("Camera centered.")
                else:
                    say("Camera servos are not connected.")
                continue

            if "camera left" in text:
                if camera_supported():
                    set_cam_tilt(cam_tilt + 15)
                    say("Camera left.")
                else:
                    say("Camera servos are not connected.")
                continue

            if "camera right" in text:
                if camera_supported():
                    set_cam_tilt(cam_tilt - 15)
                    say("Camera right.")
                else:
                    say("Camera servos are not connected.")
                continue

            if "camera up" in text:
                if camera_supported():
                    set_cam_pan(cam_pan + 15)
                    say("Camera up.")
                else:
                    say("Camera servos are not connected.")
                continue

            if "camera down" in text:
                if camera_supported():
                    set_cam_pan(cam_pan - 15)
                    say("Camera down.")
                else:
                    say("Camera servos are not connected.")
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

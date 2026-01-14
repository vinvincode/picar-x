#!/usr/bin/env python3

import os
import time
import re
import threading

from picarx import Picarx
from picarx.stt import Vosk
from picarx.tts import Piper
from picarx.llm import Ollama
from picarx.music import Music
from gpiozero import Button, LED

print("BOOT: reached python start")

# systemd services often have no login session; picarx uses os.getlogin()
# which can crash with OSError -25. Force a stable username.
try:
    os.getlogin()
except Exception:
    os.getlogin = lambda: "picar"
    os.environ.setdefault("LOGNAME", "picar")
    os.environ.setdefault("USER", "picar")
    os.environ.setdefault("HOME", "/home/picar")

usr_button = Button(25, pull_up=True)   # USR button
rst_button = Button(16, pull_up=True)   # RST button
hat_led = LED(26)     

hat_led.on()
time.sleep(0.2)
hat_led.off()

# --- OFFLINE VOSK FIX ---
# SunFounder Vosk wrapper downloads model-list.json from alphacephei.com at startup.
# Offline -> DNS fails -> program crashes. We force a local model name and skip web.
try:
    import sunfounder_voice_assistant.stt.vosk as _vosk_mod
    from pathlib import Path

    LOCAL_MODEL_NAME = "vosk-model-small-en-us-0.15"
    LOCAL_MODEL_PATH = Path("/opt/vosk_models") / LOCAL_MODEL_NAME

    def offline_update_model_list(self):
        # Provide the structures the wrapper expects, without any network call.
        self.available_languages = ["en-us"]
        self.available_model_names = [LOCAL_MODEL_NAME]
        self.available_model_urls = [""]

    def offline_get_model_name(self, lang: str) -> str:
        # Always return our local model for en-us
        return LOCAL_MODEL_NAME

    # Apply patches
    _vosk_mod.Vosk.update_model_list = offline_update_model_list
    _vosk_mod.Vosk.get_model_name = offline_get_model_name

    # Optional: fail early with a clear message if model folder missing
    if not LOCAL_MODEL_PATH.exists():
        print(f"Vosk model missing: {LOCAL_MODEL_PATH}. Install it into /opt/vosk_models/")
except Exception as _e:
    print("Offline Vosk patch failed:", _e)


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
music = Music()

# ---- Autopilot stop event (instant stop even during sleeps) ----
autopilot_stop_event = threading.Event()

def _stop_autopilot_callback():
    autopilot_stop_event.set()

usr_button.when_pressed = _stop_autopilot_callback
rst_button.when_pressed = _stop_autopilot_callback

def sleep_interruptible(seconds: float) -> bool:
    """
    Sleep in small chunks so autopilot can exit quickly when button is pressed.
    Returns False if interrupted by stop event, else True.
    """
    end = time.time() + seconds
    while time.time() < end:
        if autopilot_stop_event.is_set():
            return False
        time.sleep(0.05)
    return True


INSTRUCTIONS = (
    "You are a helpful assistant. "
    "Output must be plain text only. "
    "Do not use markdown or formatting characters. "
    "Keep answers short (1 to 3 sentences)."
)

WELCOME = (
    "Hello. Say hey wally. Say start for drive mode, ai mode for questions, "
    "or autopilot for autonomous driving and obstacle avoidance."
)
llm = None
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

# Turbo mode
turbo_on = False
TURBO_BOOST = 15  # extra speed when turbo is on (clamped)

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

# Autopilot trigger aliases
AUTOPILOT_TRIGGERS = ("autopilot", "auto pilot", "autopilot mode", "auto", "on a pilot")

# AI/Drive mishears
AI_MODE_TRIGGERS = ("ai mode", "a mod", "a more", "a mode")
DRIVE_MODE_TRIGGERS = ("drive mode", "dr more", "dr mode", "drive")

# ----------------------------
# Helpers
# ----------------------------
def get_llm():
    global llm
    if llm is not None:
        return llm
    # Create only when needed (prevents boot hangs if networking/DNS is weird)
    try:
        _llm = Ollama(ip="127.0.0.1", model="llama3.2:3b")
        _llm.set_max_messages(20)
        _llm.set_instructions(INSTRUCTIONS)
        _llm.set_welcome(WELCOME)
        llm = _llm
        return llm
    except Exception as e:
        print("LLM init failed:", e)
        return None

def led_off():
    try:
        hat_led.off()
    except Exception:
        pass

def led_listening():
    # slow blink while waiting for speech
    try:
        hat_led.blink(on_time=0.25, off_time=0.25, background=True)
    except Exception:
        pass

def led_thinking():
    # fast blink while generating
    try:
        hat_led.blink(on_time=0.08, off_time=0.08, background=True)
    except Exception:
        pass

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
    llm_obj = get_llm()
    if llm_obj is None:
        say("AI is offline right now.")
        return
    try:
        led_thinking()
        camera_nod(3)
        response = llm.prompt(question, stream=True)
    except Exception as e:
        led_off()
        say(f"Sorry, I couldn't reach the local model. {e}")
        return

    speak_streaming_response(response)
    led_off()

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

def current_drive_speed():
    boost = TURBO_BOOST if turbo_on else 0
    return int(clamp(speed + boost, 10, 60))


# ----------------------------
# Camera control (pan/tilt)
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
    start = cam_tilt
    if not camera_supported():
        return
    for _ in range(times):
        set_cam_tilt(clamp(start - 18, CAM_MIN, CAM_MAX))
        time.sleep(0.18)
        set_cam_tilt(clamp(start + 10, CAM_MIN, CAM_MAX))
        time.sleep(0.18)
    set_cam_tilt(start)

def camera_shake(times=2):
    start = cam_pan
    if not camera_supported():
        return
    for _ in range(times):
        set_cam_pan(clamp(start - 20, CAM_MIN, CAM_MAX))
        time.sleep(0.18)
        set_cam_pan(clamp(start + 20, CAM_MIN, CAM_MAX))
        time.sleep(0.18)
    set_cam_pan(start)

def camera_scan():
    if not camera_supported():
        return
    start_tilt = cam_tilt
    set_cam_tilt(start_tilt)
    for a in [-30, -15, 0, 15, 30, 0]:
        set_cam_pan(a)
        time.sleep(0.25)

# ----------------------------
# DRIVE helpers
# ----------------------------
def drive_forward_pulse():
    px.set_dir_servo_angle(STEER_CENTER)
    px.forward(current_drive_speed())
    time.sleep(PULSE_TIME)
    px.stop()

def drive_backward_pulse():
    px.set_dir_servo_angle(STEER_CENTER)
    px.backward(current_drive_speed())
    time.sleep(PULSE_TIME)
    px.stop()

def drive_left_pulse():
    px.set_dir_servo_angle(STEER_LEFT)
    px.forward(current_drive_speed())
    time.sleep(PULSE_TIME)
    px.stop()
    px.set_dir_servo_angle(STEER_CENTER)

def drive_right_pulse():
    px.set_dir_servo_angle(STEER_RIGHT)
    px.forward(current_drive_speed())
    time.sleep(PULSE_TIME)
    px.stop()
    px.set_dir_servo_angle(STEER_CENTER)

def start_circle():
    global circle_active, continuous_motion, drive_style
    circle_active = True
    drive_style = "continuous"
    continuous_motion = "FWD"
    px.set_dir_servo_angle(CIRCLE_STEER)
    px.forward(current_drive_speed())

def refresh_continuous():
    if not drive_enabled:
        return
    if circle_active:
        px.set_dir_servo_angle(CIRCLE_STEER)
        px.forward(current_drive_speed())
        return
    if continuous_motion == "FWD":
        px.set_dir_servo_angle(STEER_CENTER)
        px.forward(current_drive_speed())
    elif continuous_motion == "BACK":
        px.set_dir_servo_angle(STEER_CENTER)
        px.backward(current_drive_speed())

# ----------------------------
# Fun drive-mode actions
# ----------------------------
def fun_dance():
    px.stop()
    for ang in [-25, 25, -25, 25, 0]:
        px.set_dir_servo_angle(ang)
        px.forward(25)
        time.sleep(0.20)
        px.stop()
        time.sleep(0.08)
    px.set_dir_servo_angle(0)

def fun_lookaround():
    if camera_supported():
        camera_scan()
        camera_nod(1)
        camera_shake(1)
        camera_center()
    else:
        for ang in [-20, 20, 0]:
            px.set_dir_servo_angle(ang)
            time.sleep(0.2)

def do_drift():
    base = current_drive_speed()
    px.forward(base)
    for ang in [25, -25, 30, -30, 20, -20, 0]:
        px.set_dir_servo_angle(ang)
        time.sleep(0.16)
    px.set_dir_servo_angle(0)
    px.stop()

def do_scan():
    for ang in [-30, -15, 0, 15, 30, 0]:
        px.set_dir_servo_angle(ang)
        time.sleep(0.2)
    px.forward(25)
    time.sleep(0.5)
    px.stop()
    px.set_dir_servo_angle(0)
    say("Scan complete.")

def do_turbo_toggle():
    global turbo_on
    turbo_on = not turbo_on
    say("Turbo on." if turbo_on else "Turbo off.")


# ----------------------------
# Autopilot (button exits instantly)
# ----------------------------
def run_autopilot():
    """
    Autopilot obstacle avoidance.
    USR/RST buttons stop it instantly (even during long sleeps).
    """
    POWER = 45
    SafeDistance = 40.0
    DangerDistance = 20.0

    MAX_INVALID_READS = 30
    BACK_TIME = 0.70
    FORWARD_RECOVER_TIME = 0.35
    TURN_TIME = 0.18

    ESCAPE_LEFT_ANGLE = 35
    ESCAPE_RIGHT_ANGLE = -35

    # Exit-by-hand-cover
    COVER_EXIT_DIST = 6.0
    COVER_EXIT_TIME = 1.2
    cover_start = None

    # Voice throttle
    SAY_EVERY_S = 1.0
    last_say = 0.0
    last_action = ""

    def autopilot_say(distance, action):
        nonlocal last_say, last_action
        now = time.time()
        if now - last_say < SAY_EVERY_S and action == last_action:
            return
        last_say = now
        last_action = action
        if distance is None:
            say(f"Autopilot. {action}.")
        else:
            say(f"{action}. Distance {int(distance)} centimeters.")

    autopilot_stop_event.clear()
    hat_led.on()

    say("Autopilot. Autonomous driving and obstacle avoidance.")
    print("AUTOPILOT STARTED")

    invalid_reads = 0
    escape_side = "LEFT"

    try:
        while True:
            # immediate exit if button pressed
            if autopilot_stop_event.is_set():
                say("Exiting autopilot.")
                break

            raw = px.ultrasonic.read()
            distance = sanitize_distance(raw)

            # Cover-to-exit
            if distance is not None and distance <= COVER_EXIT_DIST:
                if cover_start is None:
                    cover_start = time.time()
                elif time.time() - cover_start >= COVER_EXIT_TIME:
                    say("Exiting autopilot.")
                    break
            else:
                cover_start = None

            if distance is None:
                invalid_reads += 1
                px.stop()

                if not sleep_interruptible(0.12):
                    say("Exiting autopilot.")
                    break
                continue

            invalid_reads = 0
            print("distance:", distance)

            if distance >= SafeDistance:
                px.set_dir_servo_angle(0)
                px.forward(POWER)
                autopilot_say(distance, "Driving forward")
                if not sleep_interruptible(0.02):
                    say("Exiting autopilot.")
                    break
                continue

            if distance >= DangerDistance:
                px.set_dir_servo_angle(30)
                px.forward(POWER)
                autopilot_say(distance, "Turning")
                if not sleep_interruptible(TURN_TIME):
                    say("Exiting autopilot.")
                    break
                continue

            # Too close: escape routine
            px.set_dir_servo_angle(-30)
            px.backward(POWER)
            autopilot_say(distance, "Backing up")
            if not sleep_interruptible(BACK_TIME):
                say("Exiting autopilot.")
                break

            px.stop()
            if not sleep_interruptible(0.08):
                say("Exiting autopilot.")
                break

            if escape_side == "LEFT":
                px.set_dir_servo_angle(ESCAPE_LEFT_ANGLE)
                escape_side = "RIGHT"
                autopilot_say(distance, "Retrying left")
            else:
                px.set_dir_servo_angle(ESCAPE_RIGHT_ANGLE)
                escape_side = "LEFT"
                autopilot_say(distance, "Retrying right")

            px.forward(POWER)
            if not sleep_interruptible(FORWARD_RECOVER_TIME):
                say("Exiting autopilot.")
                break

            px.stop()
            if not sleep_interruptible(0.08):
                say("Exiting autopilot.")
                break

    finally:
        px.stop()
        px.set_dir_servo_angle(0)
        hat_led.off()
        autopilot_stop_event.clear()
        print("AUTOPILOT ENDED")


# ----------------------------
# Help text (Drive mode)
# ----------------------------
def help_drive():
    return (
        "Drive: start, stop, forward, backward, left, right, straight, circle. "
        "Speed: faster, slower, speed 40. "
        "Modes: pulse mode, continuous mode. "
        "Fun: dance, drift, scan, turbo, look around, nod, shake head. "
        "Autopilot: say autopilot. Press USR to exit."
    )


# ----------------------------
# Main
# ----------------------------
print("BOOT: about to speak welcome")
hat_led.on()
music.music_play('../musics/mac_startup.mp3')
time.sleep(2)
music.sound_play('../sounds/car-double-horn.wav')
time.sleep(0.2)
say(WELCOME)
hat_led.off()
print(WELCOME)
print('Say "hey wally" to wake. Say "sleep" to pause. Ctrl+C to quit.')

try:
    while True:
        stt.wait_until_heard(WAKE_WORDS)
        say("Ready.")
        print("Wake word detected. Listening... (say 'sleep' to pause)")

        while True:
            if mode == MODE_DRIVE and drive_style == "continuous":
                refresh_continuous()

            if mode == MODE_AI:
                led_listening()
                signal_listening()
            else:
                led_off()

            res = stt.listen(stream=False)
            text = res.get("text", "") if isinstance(res, dict) else str(res)
            text = (text or "").lower().strip()
            if not text:
                continue

            print("Heard:", text)

            if "sleep" in text:
                stop_car(center=True)
                drive_enabled = False
                mode = MODE_DRIVE
                say("Sleeping.")
                break

            if "help" in text:
                say(help_drive())
                continue

            if any(k in text for k in AI_MODE_TRIGGERS):
                mode = MODE_AI
                drive_enabled = False
                stop_car(center=True)
                led_listening()
                say("AI mode. Ask me a question.")
                continue

            if any(k in text for k in DRIVE_MODE_TRIGGERS):
                mode = MODE_DRIVE
                drive_enabled = False
                stop_car(center=True)
                say("Drive mode.")
                continue

            if any(k in text for k in AUTOPILOT_TRIGGERS):
                stop_car(center=True)
                drive_enabled = False
                mode = MODE_DRIVE
                run_autopilot()
                say(WELCOME)
                print(WELCOME)
                continue

            if "stop" in text or "disable" in text:
                drive_enabled = False
                music.music_stop()
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

            if mode == MODE_AI:
                stop_car(center=False)
                ask_llm_and_speak(text)
                continue

            if "music" in text:
                camera_nod(3)
                say("Playing music.")
                music.music_stop()
                music.music_play('../musics/wall_e_adventure.mp3')
                continue

            if "honk" in text:
                music.sound_play('../sounds/car-double-horn.wav')
                continue

            if is_question(text):
                say("Say AI mode if you want me to answer questions.")
                continue

            if not drive_enabled:
                say("Say start first.")
                continue

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

            if "dance" in text:
                say("Dancing.")
                fun_dance()
                continue

            if "drift" in text:
                say("Drifting.")
                do_drift()
                continue

            if "scan" in text:
                do_scan()
                continue

            if "turbo" in text:
                do_turbo_toggle()
                continue

            if "look around" in text or "lookaround" in text:
                say("Looking around.")
                fun_lookaround()
                continue

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
                    px.forward(current_drive_speed())
                    continuous_motion = "FWD"
                say("Left.")
                continue

            if text == "right":
                circle_active = False
                if drive_style == "pulse":
                    drive_right_pulse()
                else:
                    px.set_dir_servo_angle(STEER_RIGHT)
                    px.forward(current_drive_speed())
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
    hat_led.off()

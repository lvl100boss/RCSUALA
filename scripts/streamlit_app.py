import sys
import json
import threading
import queue
import time
import logging
import traceback
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List
import base64
from io import BytesIO
from PIL import Image
import subprocess
import cv2
import numpy as np
import streamlit as st
from streamlit.runtime.scriptrunner import add_script_run_ctx, get_script_run_ctx

# Import project utilities
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.authorization_manager import AuthorizationManager
from utils.room_activity_logger import RoomActivityLogger
from utils.confirmation_manager import ConfirmationManager
from utils.rtsp_config_manager import get_manager as get_rtsp_manager
from utils.session_metrics import SessionMetrics


# Reduce noisy MediaFileHandler tracebacks (they can still appear on stop/refresh
# if the browser requests an older media id). This matches the old app's behavior.
logging.getLogger("streamlit.web.server.media_file_handler").setLevel(logging.ERROR)


# NOTE: We keep Streamlit media handler logs enabled while stabilizing frame rendering.

# Repo root + imports
REPO_ROOT = Path(__file__).resolve().parents[1]
ACTIVITY_LOG_PATH = REPO_ROOT / "datasets" / "logs.json"
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import camera_config_streamlit as cam_config


# -----------------------------
# UI styling — modern redesign
# -----------------------------
st.set_page_config(
    page_title="CCTV Security System",
    page_icon="🎥",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
<style>
    /* ===== Global Reset & Typography ===== */
    .main-header {
        margin-top: 0.5rem;
        font-size: 1.8rem;
        font-weight: 700;
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        text-align: center;
        margin-bottom: 0.5rem;
        letter-spacing: -0.02em;
    }

    /* ===== Sidebar cleanup ===== */
    [data-testid="stSidebar"] {
        background-color: #0e1117;
    }
    [data-testid="stSidebar"] .stButton > button[kind="primary"] {
        width: 100%;
    }
    .sidebar-section-label {
        font-size: 0.7rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        color: #8b8fa3;
        margin: 0.8rem 0 0.3rem 0;
    }

    /* ===== Detection cards ===== */
    .det-card {
        padding: 0.65rem 0.85rem;
        border-radius: 8px;
        margin: 0.35rem 0;
        font-size: 0.85rem;
        font-weight: 500;
        line-height: 1.4;
        border-left: 4px solid;
        transition: transform 0.15s ease, box-shadow 0.15s ease;
    }
    .det-card:hover { transform: translateX(2px); }
    .det-card .det-name {
        font-size: 0.95rem;
        font-weight: 700;
        margin-bottom: 2px;
    }
    .det-card .det-meta {
        font-size: 0.75rem;
        opacity: 0.85;
    }
    .det-card .det-behavior {
        font-size: 0.78rem;
        font-weight: 600;
        margin-top: 3px;
        padding: 2px 8px;
        border-radius: 4px;
        display: inline-block;
    }

    /* Authorization color variants */
    .det-authorized {
        background-color: rgba(40, 167, 69, 0.12);
        border-left-color: #28a745;
        color: #b7dfbf;
    }
    .det-authorized .det-behavior { background: rgba(40,167,69,0.2); color: #6fcf7f; }

    .det-partial {
        background-color: rgba(255, 193, 7, 0.10);
        border-left-color: #ffc107;
        color: #f5e6b8;
    }
    .det-partial .det-behavior { background: rgba(255,193,7,0.2); color: #ffd95c; }

    .det-unauthorized {
        background-color: rgba(220, 53, 69, 0.12);
        border-left-color: #dc3545;
        color: #f0b3b8;
    }
    .det-unauthorized .det-behavior { background: rgba(220,53,69,0.2); color: #ff7a85; }

    /* ===== Stat badges ===== */
    .stat-row {
        display: flex;
        gap: 8px;
        margin: 0.5rem 0;
    }
    .stat-badge {
        flex: 1;
        text-align: center;
        padding: 0.6rem 0.4rem;
        border-radius: 8px;
        font-weight: 700;
        font-size: 1.3rem;
        line-height: 1;
    }
    .stat-badge .stat-label {
        font-size: 0.65rem;
        font-weight: 500;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        opacity: 0.75;
        margin-bottom: 2px;
    }
    .stat-total  { background: rgba(102,126,234,0.15); color: #a0b4ff; }
    .stat-auth   { background: rgba(40,167,69,0.15);   color: #6fcf7f; }
    .stat-unauth { background: rgba(220,53,69,0.15);   color: #ff7a85; }

    /* ===== Alert/Notification styles ===== */
    .alert-container {
        position: fixed;
        bottom: 20px;
        right: 20px;
        z-index: 9999;
        display: flex;
        flex-direction: column-reverse;
        gap: 8px;
        max-width: 360px;
        pointer-events: none;
    }
    .alert-notification {
        padding: 0.8rem 1.2rem;
        border-radius: 8px;
        font-weight: 600;
        font-size: 0.88rem;
        box-shadow: 0 8px 24px rgba(0,0,0,0.3);
        animation: alertSlide 0.35s cubic-bezier(0.34, 1.56, 0.64, 1);
        pointer-events: auto;
        backdrop-filter: blur(8px);
    }
    .alert-unauthorized {
        background: rgba(220,53,69,0.92);
        color: #fff;
        border: 1px solid rgba(255,255,255,0.15);
    }
    .alert-partial {
        background: rgba(255,193,7,0.92);
        color: #1a1a1a;
        border: 1px solid rgba(0,0,0,0.1);
    }
    @keyframes alertSlide {
        from { transform: translateY(120%); opacity: 0; }
        to   { transform: translateY(0);    opacity: 1; }
    }

    /* ===== Status indicator ===== */
    .status-indicator {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        font-size: 0.85rem;
        font-weight: 600;
        padding: 4px 12px;
        border-radius: 20px;
    }
    .status-live {
        background: rgba(40,167,69,0.15);
        color: #6fcf7f;
    }
    .status-live::before {
        content: '';
        width: 8px; height: 8px;
        border-radius: 50%;
        background: #28a745;
        animation: pulse 1.5s infinite;
    }
    .status-stopped {
        background: rgba(220,53,69,0.15);
        color: #ff7a85;
    }
    .status-recording {
        background: rgba(220,53,69,0.2);
        color: #ff7a85;
        margin-left: 6px;
    }
    .status-recording::before {
        content: '';
        width: 8px; height: 8px;
        border-radius: 50%;
        background: #dc3545;
        animation: pulse 1s infinite;
    }
    @keyframes pulse {
        0%, 100% { opacity: 1; }
        50% { opacity: 0.3; }
    }

    /* ===== Log viewer ===== */
    .log-date-header {
        font-size: 0.8rem;
        font-weight: 700;
        color: #8b8fa3;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        padding: 0.4rem 0;
        border-bottom: 1px solid rgba(255,255,255,0.06);
        margin: 0.8rem 0 0.3rem 0;
    }

    /* ===== Misc polish ===== */
    .block-container { padding-top: 1.5rem; }

    /* ===== Live evaluation metrics bar ===== */
    .eval-bar {
        display: flex;
        gap: 6px;
        margin: 0.4rem 0 0.6rem 0;
    }
    .eval-chip {
        flex: 1;
        text-align: center;
        padding: 0.45rem 0.3rem;
        border-radius: 6px;
        font-weight: 700;
        font-size: 1.05rem;
        line-height: 1;
    }
    .eval-chip .eval-label {
        font-size: 0.55rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        opacity: 0.7;
        margin-bottom: 2px;
    }
    .eval-fps   { background: rgba(102,126,234,0.18); color: #a0b4ff; }
    .eval-det   { background: rgba(40,167,69,0.18);   color: #6fcf7f; }
    .eval-pred  { background: rgba(255,193,7,0.18);   color: #ffd95c; }
    .eval-ppl   { background: rgba(220,53,69,0.18);   color: #ff7a85; }
</style>
""",
    unsafe_allow_html=True,
)


# -----------------------------
# Session state
# -----------------------------
def _init_state(key, default):
    if key not in st.session_state:
        st.session_state[key] = default


_init_state("running", False)
_init_state("frame_queue", queue.Queue(maxsize=2))
_init_state("log_queue", queue.Queue())
_init_state("current_detections", [])
_init_state("processing_thread", None)
_init_state("stop_flag", threading.Event())
_init_state("recording_enabled", False)
_init_state("recording_path", None)
_init_state("enable_logging", False)
_init_state("all_logs", [])  # Store all logs for viewing
_init_state("session_log_file", None)  # Store current session log file path
_init_state("session_metrics", None)  # SessionMetrics instance (live during run)
_init_state("last_eval_report", None)  # Final evaluation report dict (after stop)

# Alerts
_init_state("alert_cooldown", 5)
_init_state("alert_duration", 10)
_init_state("active_alerts", {})
_init_state("last_alert", {})


# -----------------------------
# Authorization (Dynamic from AuthorizationManager)
# -----------------------------
# Initialize AuthorizationManager
if "auth_manager" not in st.session_state:
    st.session_state.auth_manager = AuthorizationManager()
    # Note: AuthorizationManager automatically syncs on initialization


def get_authorization_map():
    """Get the current authorization map with lowercase keys for case-insensitive lookup"""
    auth_map = st.session_state.auth_manager.get_all_authorizations()
    # Convert to lowercase keys for case-insensitive lookup
    return {k.lower(): v for k, v in auth_map.items()}


def get_authorization_level(identity_name: str) -> str:
    """Get authorization level for a person (case-insensitive)"""
    if not identity_name or identity_name == "Unknown":
        return "Unauthorized"
    # Get fresh map each time
    auth_map = get_authorization_map()
    return auth_map.get(str(identity_name).lower(), "Partially Authorized")


def get_authorization_color(auth_level: str):
    color_map = {
        "Authorized": (0, 255, 0),
        "Partially Authorized": (0, 165, 255),
        "Unauthorized": (0, 0, 255),
    }
    return color_map.get(auth_level, (128, 128, 128))


# -----------------------------
# UI helper functions
# -----------------------------
def _safe_put(q: queue.Queue, payload):
    try:
        # Add timestamp if it's a log message       
        if isinstance(payload, dict) and 'message' in payload and 'timestamp' not in payload:
            payload['timestamp'] = datetime.now().strftime("%H:%M:%S")
        q.put(payload)
    except Exception:
        pass


def _drain_remaining_queue():
    """Drain leftover messages from log_queue (e.g. eval_report sent during cleanup)."""
    try:
        while not st.session_state.log_queue.empty():
            msg = st.session_state.log_queue.get_nowait()
            if msg.get('type') == 'eval_report':
                st.session_state.last_eval_report = msg.get('data')
            elif msg.get('type') == 'log_file_path':
                st.session_state.session_log_file = msg.get('path')
            elif msg.get('type') == 'recording_path':
                st.session_state.recording_path = msg.get('path')
            elif msg.get('type') != 'detections':
                if 'timestamp' not in msg:
                    msg['timestamp'] = datetime.now().strftime("%H:%M:%S")
                st.session_state.all_logs.append(msg)
    except Exception:
        pass


def frame_to_base64(frame_bgr):
    """Convert OpenCV frame to base64 data URI"""
    frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    pil_img = Image.fromarray(frame_rgb)
    buffer = BytesIO()
    pil_img.save(buffer, format="JPEG", quality=85)
    img_str = base64.b64encode(buffer.getvalue()).decode()
    return f"data:image/jpeg;base64,{img_str}"


def display_loop(
    video_placeholder,
    detections_placeholder,
    total_placeholder,
    log_placeholder,
    status_placeholder,
    alert_placeholder,
    *,
    frame_sleep_s: float = 0.01,
) -> None:
    """Run a tight UI loop while `running` is True.

    This matches the old app’s approach and avoids `st.rerun()` churn,
    which is the most common trigger for Streamlit MemoryMediaFileStorage
    "Bad filename ...jpg" errors during rapid frame updates.
    """

    # We update placeholders in-place; Streamlit will stream deltas.
    # Important: do NOT call st.rerun() here. The old app stays stable by
    # avoiding rerun churn entirely during live display.
    while st.session_state.running:
        # If stop was requested from sidebar, honor it ASAP.
        if st.session_state.stop_requested:
            break

        # --- Frame
        frame = None
        try:
            if not st.session_state.frame_queue.empty():
                frame = st.session_state.frame_queue.get_nowait()
                st.session_state.last_frame = frame
        except queue.Empty:
            frame = None
        except Exception:
            frame = None

        frame_to_show = st.session_state.last_frame
        if frame_to_show is not None:
            try:
                frame_b64 = frame_to_base64(frame_to_show)
                video_placeholder.markdown(
                    f'<img src="{frame_b64}" width="640">',
                    unsafe_allow_html=True
                )
            except Exception as e:
                # The old app simply ignores MediaFileStorage-related display issues.
                if "MediaFileStorage" not in str(type(e).__name__):
                    print(f"Display error: {e}")

        # --- Log queue (also carries detections)
        log_messages = []
        while not st.session_state.log_queue.empty():
            try:
                msg = st.session_state.log_queue.get_nowait()
                if msg.get("type") == "detections":
                    st.session_state.current_detections = msg.get("data", [])
                elif msg.get("type") == "recording_path":
                    st.session_state.recording_path = msg.get("path")
                else:
                    log_messages.append(msg)
            except queue.Empty:
                break
            except Exception:
                break

        # --- Detections + alerts
        try:
            with detections_placeholder.container():
                if st.session_state.current_detections:
                    for detection in st.session_state.current_detections:
                        auth_cls = "det-authorized" if detection["authorization"] == "Authorized" else (
                            "det-partial" if detection["authorization"] == "Partially Authorized" else "det-unauthorized"
                        )

                        # Build behavior badge
                        behavior_html = ""
                        if "behavior_status" in detection and detection["behavior_status"] != "STATUS: NO INTERACTION":
                            behavior_status = detection["behavior_status"].replace("STATUS: ", "")
                            behavior_emoji = "🖐️"
                            for kw, em in [("CARRYING", "🎒"), ("LAPTOP", "💻"), ("HANDBAG", "👜"),
                                           ("CELL PHONE", "📱"), ("KEYBOARD", "⌨️"), ("MOUSE", "�️")]:
                                if kw in behavior_status:
                                    behavior_emoji = em
                            behavior_html = f'<div class="det-behavior">{behavior_emoji} {behavior_status}</div>'

                        tid = detection.get("track_id", -1)
                        tid_info = f" [ID: {tid}]" if tid != -1 else ""
                        camera_tag = detection.get("camera", "Primary")

                        st.markdown(f"""
                        <div class="det-card {auth_cls}">
                            <div class="det-name">{detection['identity']}{tid_info}</div>
                            <div class="det-meta">📷 {camera_tag} · {detection['authorization']}</div>
                            {behavior_html}
                        </div>
                        """, unsafe_allow_html=True)

                        if detection["authorization"] in ["Unauthorized", "Partially Authorized"]:
                            show_alert(detection["authorization"], detection["identity"])
                else:
                    st.info("No detections")

            alerts_html = get_active_alerts()
            if alerts_html:
                alert_placeholder.markdown(alerts_html, unsafe_allow_html=True)
            else:
                alert_placeholder.empty()
        except Exception:
            pass

        # --- Stats (custom HTML badges)
        try:
            total = len(st.session_state.current_detections)
            auth_count = sum(1 for d in st.session_state.current_detections if d["authorization"] == "Authorized")
            unauth_count = sum(1 for d in st.session_state.current_detections if d["authorization"] == "Unauthorized")
            interact_count = sum(1 for d in st.session_state.current_detections
                                 if d.get("behavior_status", "STATUS: NO INTERACTION") != "STATUS: NO INTERACTION")

            interact_badge = ""
            if interact_count > 0:
                interact_badge = (
                    f'<div class="stat-badge" style="background:rgba(255,107,53,0.15);color:#ff8c57;">'
                    f'<div class="stat-label">Interact</div>{interact_count}</div>'
                )

            total_placeholder.markdown(f"""
            <div class="stat-row">
                <div class="stat-badge stat-total"><div class="stat-label">Total</div>{total}</div>
                <div class="stat-badge stat-auth"><div class="stat-label">Auth</div>{auth_count}</div>
                <div class="stat-badge stat-unauth"><div class="stat-label">Unauth</div>{unauth_count}</div>
                {interact_badge}
            </div>
            """, unsafe_allow_html=True)
        except Exception:
            pass

        # --- Logs UI
        if log_messages:
            try:
                with log_placeholder.container():
                    for msg in log_messages[-5:]:
                        msg_type = msg.get("type", "info")
                        message = msg.get("message", "")
                        if msg_type == "error":
                            st.error(f"❌ {message}")
                        elif msg_type == "warning":
                            st.warning(f"⚠️ {message}")
                        elif msg_type == "success":
                            st.success(f"✅ {message}")
                        else:
                            st.info(f"ℹ️ {message}")
            except Exception:
                pass

        # --- Stop if worker died
        thread = st.session_state.processing_thread
        if thread and not thread.is_alive():
            st.session_state.running = False
            break

        time.sleep(float(frame_sleep_s))


def show_alert(auth_level: str, identity_name: str) -> None:
    key = f"{auth_level}:{identity_name}"
    now = time.time()
    last = st.session_state.last_alert.get(key, 0.0)
    if now - last < float(st.session_state.alert_cooldown):
        return

    if auth_level == "Unauthorized":
        msg = f"🚨 Unauthorized person detected: {identity_name}"
        typ = "unauthorized"
    else:
        msg = f"⚠️ Partially authorized person detected: {identity_name}"
        typ = "partial"

    st.session_state.active_alerts[key] = {
        "message": msg,
        "type": typ,
        "timestamp": now,
    }
    st.session_state.last_alert[key] = now


def get_active_alerts() -> str:
    now = time.time()
    duration = float(st.session_state.alert_duration)

    to_remove = []
    for k, v in st.session_state.active_alerts.items():
        if now - float(v.get("timestamp", 0)) > duration:
            to_remove.append(k)
    for k in to_remove:
        st.session_state.active_alerts.pop(k, None)

    if not st.session_state.active_alerts:
        return ""

    items = []
    for v in st.session_state.active_alerts.values():
        cls = "alert-unauthorized" if v.get("type") == "unauthorized" else "alert-partial"
        items.append(f"<div class=\"alert-notification {cls}\">{v.get('message','')}</div>")
    return f"<div class=\"alert-container\">{''.join(items)}</div>"


def format_source_label(source, source_mode: str) -> str:
    if source_mode == "webcam":
        return f"webcam_{source}"
    if source_mode == "rtsp":
        return "rtsp"
    try:
        p = Path(str(source))
        return p.stem
    except Exception:
        return "video"


def open_video_capture(source_mode: str, video_source):
    if source_mode == "webcam":
        cap = cv2.VideoCapture(int(video_source), cv2.CAP_DSHOW)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        cap.set(cv2.CAP_PROP_FPS, 30)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap
    if source_mode == "rtsp":
        cap = cv2.VideoCapture(str(video_source))
        cap.set(cv2.CAP_PROP_BUFFERSIZE, cam_config.RTSP_BUFFER_SIZE)
        for _ in range(10):
            cap.grab()
        return cap
    return cv2.VideoCapture(str(video_source))


def video_processing_thread(video_source, config, frame_queue, log_queue, stop_flag):
    """YOLO + ByteTrack tracking + FaceNet recognition + optional behavior detection."""

    cap = None
    cap2 = None  # Initialize secondary camera
    recorder = None
    pipeline = None
    session_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # Evidence saving for interactions - organized by session
    last_saved = {}  # track_id -> last save timestamp
    SAVE_COOLDOWN = 3.0  # seconds between saves per track ID
    
    # Create session-specific evidence folder
    source_mode = config.get("source_mode", "webcam")
    source_label = format_source_label(video_source, source_mode)
    evidence_dir = REPO_ROOT / "office_evidence" / f"{source_label}_{session_timestamp}"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    
    # Create log file in LOGS folder organized by date (separate from evidence)
    current_date = datetime.now().strftime("%m-%d-%Y")  # Format: 02-19-2026
    logs_date_dir = REPO_ROOT / "logs" / current_date
    logs_date_dir.mkdir(parents=True, exist_ok=True)
    log_file_path = logs_date_dir / f"session_{source_label}_{session_timestamp}.txt"
    
    def write_to_log_file(message: str):
        """Write a log entry to the session log file"""
        try:
            timestamp = datetime.now().strftime("%B %d, %Y at %I:%M:%S %p")
            with open(log_file_path, 'a', encoding='utf-8') as f:
                f.write(f"[{timestamp}] {message}\n")
        except Exception:
            pass
    
    # Write session header
    write_to_log_file("="*60)
    write_to_log_file(f"CCTV Monitoring Session Started")
    write_to_log_file(f"Source: {source_label}")
    write_to_log_file(f"Evidence Folder: {evidence_dir.name}")
    write_to_log_file(f"Log File: {current_date}/{log_file_path.name}")
    write_to_log_file("="*60)
    
    # Send log file path to UI
    _safe_put(log_queue, {"type": "log_file_path", "path": str(log_file_path)})
    
    # Log evidence and log folder locations for easy finding
    _safe_put(log_queue, {"type": "success", "message": f"📁 Evidence folder: {evidence_dir.name}/"})
    _safe_put(log_queue, {"type": "success", "message": f"📄 Log file: logs/{current_date}/{log_file_path.name}"})
    
    # Check if logging is enabled
    enable_logging = config.get("enable_logging", False)
    
    # Room activity logger (writes to datasets/logs.json)
    activity_logger = RoomActivityLogger(ACTIVITY_LOG_PATH)

    # Confirmation manager — temporal smoothing to prevent spammy logs.
    # Detections must be stable for CONFIRM_FRAMES consecutive frames
    # before the logger ever sees them.
    confirmer = ConfirmationManager(
        confirm_frames=15,       # ~0.5s at 30fps before confirming identity
        gone_grace_frames=45,    # ~1.5s occlusion tolerance before "left"
        name_lock_hits=3,        # FaceNet must agree 3 times on a name
        ema_alpha=0.3,           # Confidence smoothing factor
    )

    # Session-level evaluation metrics
    metrics = SessionMetrics()
    # Share with UI thread via session state (thread-safe reads)
    import streamlit as _st
    _st.session_state.session_metrics = metrics

    try:
        # Simplified startup - no technical logs
        # Only log critical errors or user-facing events
        
        # Import heavy dependencies inside thread
        if config.get("force_cpu"):
            import os
            os.environ["CUDA_VISIBLE_DEVICES"] = ""

        # Choose pipeline based on behavior detection setting
        enable_behavior = config.get("enable_behavior", False)
        if enable_behavior:
            from combined_yolo_facenet_behavior import CombinedYOLOFaceNetBehavior as PipelineClass
        else:
            from combined_yolo_facenet_only import CombinedYOLOFaceOnly as PipelineClass

        # Choose which FaceNet module file to load (current vs old) if provided.
        facenet_main_path = config.get("facenet_main") or (REPO_ROOT / "face_recognition" / "Facenet" / "facenet_main.py")

        # YOLO model path
        yolo_path = Path(config.get("yolo_model", ""))
        if not yolo_path.is_absolute():
            yolo_path = (REPO_ROOT / yolo_path).resolve()

        if not yolo_path.exists():
            _safe_put(log_queue, {"type": "error", "message": "❌ System Error: Camera model not found. Please contact administrator."})
            return

        # Build pipeline with behavior parameters if enabled
        # Get authorization map from config (already lowercase keys)
        auth_map_lowercase = config.get('authorization_map', {})
        
        pipeline_kwargs = {
            "yolo_model_path": str(yolo_path),
            "facenet_main_path": str(facenet_main_path),
            "authorization_map": auth_map_lowercase,
            "conf_threshold": float(config.get("conf_threshold", 0.45)),
            "resize_factor": float(config.get("resize_factor", 1.0)),
            "frame_skip": int(config.get("frame_skip", 1)),
            "recog_interval": int(config.get("recog_interval", 10)),
            "device": ("cpu" if bool(config.get("force_cpu")) else None),
        }

        if enable_behavior:
            pipeline_kwargs.update({
                "enable_behavior": True,
                "coverage_thresh": float(config.get("coverage_thresh", 0.18)),
                "move_px_thresh": float(config.get("move_px_thresh", 8.0)),
                "stationary_frames_required": int(config.get("stationary_frames_required", 6)),
                "status_on_frames_required": int(config.get("status_on_frames_required", 6)),
                "status_off_frames_required": int(config.get("status_off_frames_required", 12)),
                "object_hold_frames": int(config.get("object_hold_frames", 8)),
            })

        pipeline = PipelineClass(**pipeline_kwargs)

        # === DUAL CAMERA SETUP ===
        enable_dual_cam = config.get("enable_dual_cam", False)
        secondary_source = config.get("secondary_source")
        secondary_mode = config.get("secondary_mode")
        
        source_mode = config.get("source_mode", "webcam")
        cap = open_video_capture(source_mode, video_source)
        if cap is None or not cap.isOpened():
            _safe_put(log_queue, {"type": "error", "message": "❌ Failed to connect to primary camera."})
            return

        cap2 = None
        if enable_dual_cam and secondary_source is not None:
            _safe_put(log_queue, {"type": "info", "message": f"🔗 Connecting to secondary camera ({secondary_mode})..."})
            cap2 = open_video_capture(secondary_mode, secondary_source)
            if cap2 is None or not cap2.isOpened():
                _safe_put(log_queue, {"type": "warning", "message": "⚠️ Failed to connect to secondary camera. Running single camera mode."})
                cap2 = None
                enable_dual_cam = False
            else:
                _safe_put(log_queue, {"type": "success", "message": "✅ Dual camera mode active! Monitoring both feeds side-by-side."})

        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 640)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 480)
        fps = max(int(cap.get(cv2.CAP_PROP_FPS) or 25), 1)
        
        # Get secondary camera dimensions if dual mode
        width2, height2 = width, height
        if cap2 is not None:
            width2 = int(cap2.get(cv2.CAP_PROP_FRAME_WIDTH) or 640)
            height2 = int(cap2.get(cv2.CAP_PROP_FRAME_HEIGHT) or 480)
        
        # No startup message - system is ready, logs will show when people are detected

        # Recording - Separate videos for dual camera mode
        recorder2 = None
        if bool(config.get("recording_enabled")):
            source_label = format_source_label(video_source, source_mode)
            # Create session-specific recording folder (matching office_evidence structure)
            rec_dir = REPO_ROOT / "recordings" / f"{source_label}_{session_timestamp}"
            rec_dir.mkdir(parents=True, exist_ok=True)
            
            # Dual camera mode: Create TWO separate video files
            if enable_dual_cam and cap2 is not None:
                # Primary camera recording
                output_path = rec_dir / f"recording_primary_{session_timestamp}.mp4"
                writer = cv2.VideoWriter(str(output_path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
                
                # Secondary camera recording
                output_path2 = rec_dir / f"recording_secondary_{session_timestamp}.mp4"
                writer2 = cv2.VideoWriter(str(output_path2), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width2, height2))
                
                if writer.isOpened() and writer2.isOpened():
                    recorder = writer
                    recorder2 = writer2
                    _safe_put(log_queue, {"type": "recording_path", "path": str(output_path)})
                    _safe_put(log_queue, {"type": "success", "message": f"📹 Recording to: {rec_dir.name}/ (2 cameras)"})
                else:
                    if writer.isOpened():
                        writer.release()
                    if writer2.isOpened():
                        writer2.release()
                    _safe_put(log_queue, {"type": "error", "message": "Failed to initialize dual camera recording"})
            else:
                # Single camera recording
                output_path = rec_dir / f"recording_{session_timestamp}.mp4"
                writer = cv2.VideoWriter(str(output_path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
                if writer.isOpened():
                    recorder = writer
                    _safe_put(log_queue, {"type": "recording_path", "path": str(output_path)})
                    _safe_put(log_queue, {"type": "success", "message": f"📹 Recording to: {rec_dir.name}/"})
                else:
                    writer.release()

        while not stop_flag.is_set():
            # === READ FROM CAMERA(S) ===
            ret, frame = cap.read()
            if not ret or frame is None:
                time.sleep(0.05)
                continue
            
            frame2 = None
            if enable_dual_cam and cap2 is not None:
                ret2, frame2 = cap2.read()
                if not ret2 or frame2 is None:
                    frame2 = None  # Fallback to single camera if secondary fails
            
            # === PROCESS FRAME(S) ===
            if enable_dual_cam and frame2 is not None:
                # DUAL CAMERA MODE: Stitch frames side-by-side
                h1, w1 = frame.shape[:2]
                h2, w2 = frame2.shape[:2]
                
                # Normalize heights for side-by-side stitching
                target_h = min(h1, h2)
                if h1 != target_h:
                    new_w1 = int(w1 * target_h / h1)
                    frame1_resized = cv2.resize(frame, (new_w1, target_h))
                else:
                    frame1_resized = frame
                    new_w1 = w1
                
                if h2 != target_h:
                    new_w2 = int(w2 * target_h / h2)
                    frame2_resized = cv2.resize(frame2, (new_w2, target_h))
                else:
                    frame2_resized = frame2
                    new_w2 = w2
                
                # Stitch horizontally
                stitched_frame = np.hstack([frame1_resized, frame2_resized])
                
                # IMPORTANT: Scale down stitched frame for better object detection
                # Stitched frames are 2x wider, making objects appear smaller
                # Apply additional scaling to maintain detection quality
                stitch_h, stitch_w = stitched_frame.shape[:2]
                if stitch_w > 800:  # If stitched width is large
                    scale_factor = 800 / stitch_w
                    scaled_w = int(stitch_w * scale_factor)
                    scaled_h = int(stitch_h * scale_factor)
                    stitched_frame_scaled = cv2.resize(stitched_frame, (scaled_w, scaled_h))
                    # Store scaling info for bbox adjustment
                    bbox_scale_x = stitch_w / scaled_w
                    bbox_scale_y = stitch_h / scaled_h
                else:
                    stitched_frame_scaled = stitched_frame
                    bbox_scale_x = 1.0
                    bbox_scale_y = 1.0
                
                # Process scaled stitched frame
                annotated, detections = pipeline.process_frame(stitched_frame_scaled)
                
                # Resize annotated frame back to original stitched size for display
                if bbox_scale_x != 1.0 or bbox_scale_y != 1.0:
                    annotated = cv2.resize(annotated, (stitch_w, stitch_h))
                
                # Adjust bboxes back to original stitched frame coordinates and determine camera
                for detection in detections:
                    bbox = detection.get("bbox", (0, 0, 0, 0))
                    x1, y1, x2, y2 = bbox
                    
                    # Scale bbox back to original stitched frame size
                    x1_scaled = int(x1 * bbox_scale_x)
                    y1_scaled = int(y1 * bbox_scale_y)
                    x2_scaled = int(x2 * bbox_scale_x)
                    y2_scaled = int(y2 * bbox_scale_y)
                    detection["bbox"] = (x1_scaled, y1_scaled, x2_scaled, y2_scaled)
                    
                    center_x = (x1_scaled + x2_scaled) / 2
                    
                    # If center_x is in the left half, it's Primary camera, otherwise Secondary
                    if center_x < new_w1:
                        detection["camera"] = "Primary"
                    else:
                        detection["camera"] = "Secondary"
                        # Adjust bbox coordinates to be relative to frame2
                        detection["bbox_secondary"] = (
                            max(0, x1 - new_w1),
                            y1,
                            max(0, x2 - new_w1),
                            y2
                        )
            else:
                # SINGLE CAMERA MODE
                annotated, detections = pipeline.process_frame(frame)
                for detection in detections:
                    detection["camera"] = "Primary"
            _safe_put(log_queue, {"type": "detections", "data": detections})

            # --- Session metrics: record this frame's detections ---
            metrics.tick_frame(detections)

            # --- Confirmation manager: temporal smoothing ---
            # Run every detection through the confirmer so identity/auth
            # are only accepted after consistent multi-frame observation.
            seen_track_ids = set()
            for det in detections:
                tid = det.get("track_id", -1)
                if tid >= 0:
                    seen_track_ids.add(tid)
                result = confirmer.update(det)
                # Patch detection with confirmed (smoothed) values
                if result["confirmed"]:
                    det["identity"] = result["identity"]
                    det["authorization"] = result["authorization"]
                    det["identity_conf"] = result["confidence"]

            # Get only confirmed detections for logging (filters out flickers)
            confirmed_detections = confirmer.get_confirmed_detections(detections)

            # Check for tracks that left (grace period expired)
            left_events = confirmer.finish_frame(seen_track_ids)

            # Track current status for each person
            current_people = {}  # track_id -> {name, auth, behavior}
            
            # --- Room activity logging (de-duplicated, writes to datasets/logs.json) ---
            if enable_logging and confirmed_detections:
                for detection in confirmed_detections:
                    track_id = detection.get("track_id", -1)
                    current_people[track_id] = {
                        'name': detection.get("identity", "Unknown"),
                        'auth': detection.get("authorization", "Unauthorized"),
                        'behavior': detection.get("behavior_status", "STATUS: NO INTERACTION"),
                    }

                new_entries = activity_logger.update(confirmed_detections)
                for entry in new_entries:
                    _safe_put(log_queue, {"type": "info", "message": entry})
                    write_to_log_file(entry)
            elif enable_logging:
                # No confirmed detections this frame — still let the logger track absences
                new_entries = activity_logger.update([])
                for entry in new_entries:
                    _safe_put(log_queue, {"type": "info", "message": entry})
                    write_to_log_file(entry)
            
            # Save evidence for interactions (similar to main.py)
            if config.get("enable_behavior", False):
                current_time = time.time()
                for detection in detections:
                    tid = detection.get("track_id", -1)
                    behavior = detection.get("behavior_status", "STATUS: NO INTERACTION")
                    
                    if tid != -1 and behavior != "STATUS: NO INTERACTION":
                        last_save_time = last_saved.get(tid, 0)
                        
                        if current_time - last_save_time >= SAVE_COOLDOWN:
                            # Extract object name from behavior status
                            if "CARRYING" in behavior:
                                obj_name = behavior.replace("STATUS: CARRYING ", "").lower().replace(" ", "_")
                            elif "INTERACTING WITH" in behavior:
                                obj_name = behavior.replace("STATUS: INTERACTING WITH ", "").lower().replace(" ", "_")
                            else:
                                obj_name = "unknown"
                            
                            timestamp_str = datetime.now().strftime("%Y%m%d-%H%M%S")
                            identity = detection.get("identity", "unknown")
                            filename = evidence_dir / f"alert_{timestamp_str}_{obj_name}_{identity}_ID{tid}.jpg"
                            
                            try:
                                cv2.imwrite(str(filename), annotated)
                                last_saved[tid] = current_time
                                # No logging for evidence saving - backend operation
                            except Exception as e:
                                # Silent failure - no need to spam user with backend errors
                                pass

            if recorder is not None:
                # Dual camera mode: Write to BOTH separate recorders (original frames, not stitched)
                if enable_dual_cam and recorder2 is not None and frame2 is not None:
                    # Write primary camera frame (original, before stitching)
                    if frame.shape[1] != width or frame.shape[0] != height:
                        recorder.write(cv2.resize(frame, (width, height)))
                    else:
                        recorder.write(frame)
                    
                    # Write secondary camera frame (original)
                    if frame2.shape[1] != width2 or frame2.shape[0] != height2:
                        recorder2.write(cv2.resize(frame2, (width2, height2)))
                    else:
                        recorder2.write(frame2)
                else:
                    # Single camera recording
                    if annotated.shape[1] != width or annotated.shape[0] != height:
                        recorder.write(cv2.resize(annotated, (width, height)))
                    else:
                        recorder.write(annotated)

            try:
                while not frame_queue.empty():
                    try:
                        frame_queue.get_nowait()
                    except queue.Empty:
                        break
                frame_queue.put(annotated)
            except Exception:
                pass

            time.sleep(0.001)

        # Write session end to log file
        write_to_log_file("="*60)
        write_to_log_file("CCTV Monitoring Session Ended")
        write_to_log_file("="*60)

        # --- System Evaluation Report ---
        eval_report = metrics.report()
        eval_text = metrics.report_text()
        write_to_log_file("")
        write_to_log_file(eval_text)
        # Save report as JSON alongside session log
        try:
            import json as _json
            eval_json_path = logs_date_dir / f"evaluation_{source_label}_{session_timestamp}.json"
            with open(eval_json_path, "w", encoding="utf-8") as _ef:
                _json.dump(eval_report, _ef, indent=2)
            write_to_log_file(f"Evaluation report saved to: {eval_json_path.name}")
        except Exception:
            pass
        # Share with UI so it can render the report after session stops
        _safe_put(log_queue, {"type": "eval_report", "data": eval_report})
        _safe_put(log_queue, {"type": "success", "message": f"📊 Evaluation: Detection {eval_report['detection_rate_pct']}% · Prediction {eval_report['prediction_rate_pct']}% · {eval_report['avg_fps']} FPS"})

        # Flush room activity logger (marks remaining people as left)
        activity_logger.close()
        
        # NEW: Save unauthorized action analysis
        if pipeline is not None and enable_behavior:
            try:
                # Get summary statistics
                summary = pipeline.get_unauthorized_summary()
                spam_summary = pipeline.get_behavior_spam_summary()
                
                # Log summary to session log
                write_to_log_file("")
                write_to_log_file("="*60)
                write_to_log_file("UNAUTHORIZED ACTIONS SUMMARY")
                write_to_log_file("="*60)
                write_to_log_file(f"Total frames with unauthorized actions: {summary['total_frames_with_unauthorized']}")
                write_to_log_file(f"Total unauthorized actions: {summary['total_unauthorized_actions']}")
                write_to_log_file(f"Average per frame: {summary['avg_per_frame']}")
                write_to_log_file(f"Max in single frame: {summary['max_in_single_frame']}")
                
                if summary['by_object_type']:
                    write_to_log_file("")
                    write_to_log_file("Breakdown by object type:")
                    for obj_type, count in summary['by_object_type'].items():
                        write_to_log_file(f"  {obj_type}: {count} violations")
                
                if summary['by_person']:
                    write_to_log_file("")
                    write_to_log_file("Breakdown by person ID:")
                    for person_id, count in summary['by_person'].items():
                        write_to_log_file(f"  Person {person_id}: {count} violations")
                
                if spam_summary:
                    write_to_log_file("")
                    write_to_log_file("="*60)
                    write_to_log_file("REPEATED BEHAVIOR ALERTS (SPAM/ABNORMAL)")
                    write_to_log_file("="*60)
                    for track_id, info in spam_summary.items():
                        write_to_log_file(f"Person {track_id}: {info['last_behavior']}")
                        write_to_log_file(f"  Alert count: {info['alert_count']} times")
                        write_to_log_file(f"  Last flagged at frame: {info['last_alert_frame']}")
                
                # Save detailed JSON logs for analysis
                json_log_path = logs_date_dir / f"unauthorized_actions_{source_label}_{session_timestamp}.json"
                pipeline.save_unauthorized_logs_to_file(json_log_path)
                write_to_log_file("")
                write_to_log_file(f"Detailed analysis saved to: {json_log_path.name}")
                write_to_log_file("="*60)
                
                # Send summary to UI
                _safe_put(log_queue, {"type": "success", "message": f"📊 Analysis: {summary['total_unauthorized_actions']} unauthorized actions detected"})
                if spam_summary:
                    _safe_put(log_queue, {"type": "info", "message": f"🔁 {len(spam_summary)} person(s) with repeated behavior patterns"})
                
            except Exception as e:
                write_to_log_file(f"Failed to save analysis: {str(e)}")

    except Exception as e:
        # Only log critical user-facing errors
        error_msg = "⚠️ System encountered an error. Please restart."
        _safe_put(log_queue, {"type": "error", "message": error_msg})
        write_to_log_file(f"ERROR: {error_msg}")

    finally:
        activity_logger.close()
        if cap is not None:
            cap.release()
        if cap2 is not None:
            cap2.release()
        if recorder is not None:
            recorder.release()
        if recorder2 is not None:
            recorder2.release()


# ------------------------------------------------------------------
# Activity Log Viewer (reads from datasets/logs.json)
# ------------------------------------------------------------------

def _load_activity_logs() -> Dict:
    """Read the activity log JSON file. Returns {"logs": {date: [entries]}}."""
    if ACTIVITY_LOG_PATH.exists() and ACTIVITY_LOG_PATH.stat().st_size > 0:
        try:
            with open(ACTIVITY_LOG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "logs" in data:
                return data["logs"]
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _fmt_date(iso_date: str) -> str:
    """Convert 'YYYY-MM-DD' to 'Month DD, YYYY' for display."""
    try:
        return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%B %d, %Y")
    except ValueError:
        return iso_date


@st.dialog("📹 Configure RTSP Cameras", width="large")
def _rtsp_camera_dialog() -> None:
    """Centered modal dialog for configuring the two fixed RTSP cameras (Cam1 & Cam2)."""
    mgr = get_rtsp_manager()

    FIXED_CAMS = ["Cam1", "Cam2"]
    # Ensure both entries exist in the manager (empty defaults)
    for cam_name in FIXED_CAMS:
        if mgr.get_camera(cam_name) is None:
            mgr.add_camera(cam_name, ip="", username="", password="", remember=True)

    st.caption("Configure the RTSP connection for each camera slot. Both are saved automatically.")

    col1, col2 = st.columns(2, gap="large")

    for idx, (cam_name, col) in enumerate(zip(FIXED_CAMS, [col1, col2])):
        cam_data = mgr.get_camera(cam_name)

        with col:
            is_enabled = cam_data.get("enabled", True) if cam_data else True
            status = "🟢" if (is_enabled and cam_data and cam_data.get("ip")) else "⚫"
            st.markdown(f"#### {status} {cam_name}")

            ip = st.text_input("IP Address", value=cam_data.get("ip", "") if cam_data else "",
                               placeholder="192.168.1.10", key=f"dlg_{cam_name}_ip")
            c1, c2 = st.columns([2, 1])
            port = c1.text_input("Port", value=cam_data.get("port", "554") if cam_data else "554",
                                 key=f"dlg_{cam_name}_port")
            stream = c2.text_input("Stream", value=cam_data.get("stream", "stream2") if cam_data else "stream2",
                                   key=f"dlg_{cam_name}_stream",
                                   help="stream1 = 1080p, stream2 = 480p")
            user = st.text_input("Username", value=cam_data.get("username", "") if cam_data else "",
                                 key=f"dlg_{cam_name}_user")
            pwd = st.text_input("Password", type="password",
                                value=cam_data.get("password", "") if cam_data else "",
                                key=f"dlg_{cam_name}_pass")
            enabled = st.checkbox("✅ Enabled", value=is_enabled, key=f"dlg_{cam_name}_en")

            # URL preview
            if ip and ip.strip():
                proto = cam_data.get("protocol", "rtsp") if cam_data else "rtsp"
                u = user.strip() if user else ""
                h = f"{ip.strip()}:{port.strip()}" if port else f"{ip.strip()}:554"
                s = stream.strip() if stream else "stream2"
                preview = f"`{proto}://{u}:••••@{h}/{s}`" if u else f"`{proto}://{h}/{s}`"
                st.caption(f"**URL:** {preview}")
            else:
                st.caption("⚠️ No IP set — camera inactive")

            if idx < len(FIXED_CAMS) - 1:
                pass  # visual separator handled by columns

    # ---------- Save button ----------
    if mgr.has_keyring:
        st.caption("🔒 Passwords stored in OS keyring")
    else:
        st.caption("🔑 Passwords stored locally (base64-encoded)")

    st.divider()
    if st.button("💾 Save Both Cameras", type="primary", use_container_width=True, key="dlg_btn_save_all"):
        for cam_name in FIXED_CAMS:
            ip_val = st.session_state.get(f"dlg_{cam_name}_ip", "").strip()
            port_val = st.session_state.get(f"dlg_{cam_name}_port", "554").strip()
            stream_val = st.session_state.get(f"dlg_{cam_name}_stream", "stream2").strip()
            user_val = st.session_state.get(f"dlg_{cam_name}_user", "").strip()
            pass_val = st.session_state.get(f"dlg_{cam_name}_pass", "")
            en_val = st.session_state.get(f"dlg_{cam_name}_en", True)

            mgr.add_camera(
                cam_name, ip=ip_val, port=port_val,
                username=user_val, password=pass_val,
                stream=stream_val or "stream2",
                enabled=en_val, remember=True,
            )
        st.success("✅ Saved **Cam1** & **Cam2**")
        time.sleep(0.6)
        st.rerun()


def _render_rtsp_camera_manager() -> None:
    """Sidebar button that opens the RTSP camera management dialog."""
    if st.button("⚙ Manage Cameras", use_container_width=True, key="btn_open_cam_mgr"):
        _rtsp_camera_dialog()


def _render_eval_report(report: Dict[str, Any]) -> None:
    """Render the system evaluation report as styled Streamlit UI."""
    if not report:
        return

    # --- KPI row (styled HTML to match dashboard theme) ---
    duration = report.get("session_duration", "—")
    det_pct = report.get("detection_rate_pct", 0)
    pred_pct = report.get("prediction_rate_pct", 0)
    avg_fps = report.get("avg_fps", 0)
    total_frames = report.get("total_frames_processed", 0)

    st.markdown(f"""
    <div class="eval-bar" style="margin-bottom:1rem;">
        <div class="eval-chip eval-fps" style="padding:0.7rem 0.4rem;font-size:1.25rem;">
            <div class="eval-label">⏱ Duration</div>{duration}
        </div>
        <div class="eval-chip eval-det" style="padding:0.7rem 0.4rem;font-size:1.25rem;">
            <div class="eval-label">🎯 Detection Rate</div>{det_pct}%
        </div>
        <div class="eval-chip eval-pred" style="padding:0.7rem 0.4rem;font-size:1.25rem;">
            <div class="eval-label">🧠 Prediction Rate</div>{pred_pct}%
        </div>
        <div class="eval-chip eval-fps" style="padding:0.7rem 0.4rem;font-size:1.25rem;">
            <div class="eval-label">⚡ Avg FPS</div>{avg_fps}
        </div>
    </div>
    """, unsafe_allow_html=True)

    st.caption(f"📐 Total frames processed: **{total_frames}**")

    st.divider()

    col_det, col_pred = st.columns(2, gap="large")

    with col_det:
        st.markdown("##### 🎯 Detection Rate")
        st.caption("How often YOLO detected at least one person in a frame.")
        total_f = report.get("total_frames_processed", 0)
        with_p = report.get("frames_with_people", 0)
        pct_det = report.get("detection_rate_pct", 0)
        st.progress(min(pct_det / 100.0, 1.0))
        st.markdown(f"**{with_p}** / {total_f} frames → **{pct_det}%**")

    with col_pred:
        st.markdown("##### 🧠 Prediction Rate")
        st.caption("How often FaceNet successfully identified a detected person (not 'Unknown').")
        total_pd = report.get("total_person_detections", 0)
        ident = report.get("identified_detections", 0)
        pct_pred = report.get("prediction_rate_pct", 0)
        st.progress(min(pct_pred / 100.0, 1.0))
        st.markdown(f"**{ident}** / {total_pd} detections → **{pct_pred}%**")

    st.divider()

    col_auth, col_behav, col_ppl = st.columns(3, gap="medium")

    with col_auth:
        st.markdown("##### 🔐 Authorization")
        st.markdown(f"- 🟢 Authorized: **{report.get('auth_authorized', 0)}**")
        st.markdown(f"- 🟡 Partial: **{report.get('auth_partial', 0)}**")
        st.markdown(f"- 🔴 Unauthorized: **{report.get('auth_unauthorized', 0)}**")

    with col_behav:
        st.markdown("##### 🖐️ Interactions")
        st.markdown(f"- Total: **{report.get('total_interactions', 0)}**")
        st.markdown(f"- Unauthorized: **{report.get('unauthorized_interactions', 0)}**")

    with col_ppl:
        st.markdown("##### 👥 People Identified")
        people_list = report.get("unique_people_list", [])
        if people_list:
            for name in people_list:
                st.markdown(f"- {name.title()}")
        else:
            st.caption("No one identified this session.")


def _extract_person_name(entry: str) -> str:
    """Extract the person name from a log entry like 'HH:MM:SS - Name has entered'."""
    # Strip timestamp prefix "HH:MM:SS - "
    after_ts = entry.split(" - ", 1)[-1] if " - " in entry else entry
    # The name is everything before the first known verb phrase
    for verb in (" has entered", " is present", " is interacting", " has left"):
        idx = after_ts.find(verb)
        if idx != -1:
            return after_ts[:idx].strip()
    return ""


def _render_activity_log_viewer() -> None:
    """Streamlit component: date + person selectors, search box, filtered log entries."""
    logs = _load_activity_logs()
    available_dates = sorted(logs.keys(), reverse=True)

    if not available_dates:
        st.info("No activity logs yet. Start the system to begin logging.")
        return

    # --- Collect all unique person names across every date ---
    ALL_LABEL = "All"
    all_names: set = set()
    for entries in logs.values():
        for e in entries:
            name = _extract_person_name(e)
            if name and name != "Unknown person":
                all_names.add(name)
    person_options = [ALL_LABEL] + sorted(all_names, key=str.lower)

    # --- Controls row: date | person | search ---
    ALL_DATES_LABEL = "All Dates"
    date_options = [ALL_DATES_LABEL] + available_dates

    ctrl_col1, ctrl_col2, ctrl_col3 = st.columns([1, 1, 2])

    with ctrl_col1:
        today_str = datetime.now().strftime("%Y-%m-%d")
        if today_str in available_dates:
            default_idx = date_options.index(today_str)
        else:
            default_idx = 0
        selected = st.selectbox(
            "📅 Date",
            options=date_options,
            index=default_idx,
            format_func=lambda d: d if d == ALL_DATES_LABEL else _fmt_date(d),
            key="activity_log_date",
        )

    with ctrl_col2:
        selected_person = st.selectbox(
            "👤 Person",
            options=person_options,
            index=0,
            key="activity_log_person",
        )

    with ctrl_col3:
        search_query = st.text_input(
            "🔍 Search",
            placeholder="e.g. laptop, interacting, 10:12",
            key="activity_log_search",
        )

    query_lower = search_query.lower().strip() if search_query else ""
    show_all_dates = selected == ALL_DATES_LABEL
    filter_person = selected_person != ALL_LABEL

    # --- Build list of (date, entries) to display ---
    dates_to_show = available_dates if show_all_dates else [selected]
    total_entries = 0
    results: List[tuple] = []

    for date_key in dates_to_show:
        entries = logs.get(date_key, [])
        # Person filter (exact match on extracted name)
        if filter_person:
            entries = [e for e in entries if _extract_person_name(e) == selected_person]
        # Free-text search
        if query_lower:
            entries = [e for e in entries if query_lower in e.lower()]
        if entries:
            results.append((date_key, entries))
            total_entries += len(entries)

    # --- Summary ---
    scope = "all dates" if show_all_dates else f"**{_fmt_date(selected)}**"
    person_scope = f" for **{selected_person}**" if filter_person else ""
    st.caption(f"Showing **{total_entries}** entries on {scope}{person_scope}")

    if not results:
        if query_lower or filter_person:
            st.warning(f'No matching entries found.')
        else:
            st.info(f"No activity recorded on {scope}.")
        return

    # --- Render grouped by date ---
    for date_key, entries in results:
        st.markdown(f"**📆 {_fmt_date(date_key)}**")
        st.code("\n".join(entries), language=None)

    # --- Download ---
    download_lines: List[str] = []
    for date_key, entries in results:
        download_lines.append(f"=== {_fmt_date(date_key)} ===")
        download_lines.extend(entries)
        download_lines.append("")
    download_text = "\n".join(download_lines)

    file_label = "all_dates" if show_all_dates else selected
    person_label = f"_{selected_person}" if filter_person else ""
    st.download_button(
        label="💾 Download Logs",
        data=download_text,
        file_name=f"activity_log_{file_label}{person_label}.txt",
        mime="text/plain",
        use_container_width=True,
    )


def main():
    st.markdown('<div class="main-header">🎥 CCTV Monitoring System</div>', unsafe_allow_html=True)

    with st.sidebar:
        # ===== CONTROL BUTTONS — always visible at top =====
        col1, col2 = st.columns(2)
        with col1:
            start_button = st.button("▶️ Start", use_container_width=True, type="primary", disabled=st.session_state.running)
        with col2:
            stop_button = st.button("⏹️ Stop", use_container_width=True, disabled=not st.session_state.running)

        # Quick toggles (always visible)
        enable_recording = st.checkbox("🔴 Record", value=True, help="Save annotated video")
        enable_logging = st.checkbox("� Logging", value=True, help="Enable activity logging to logs.json")
        st.session_state.enable_logging = enable_logging

        st.divider()

        # ===== 1. VIDEO SOURCE (most common config) =====
        with st.expander("� Video Source", expanded=True):
            source_type = st.radio("Source", ["Webcam", "Video File", "RTSP Camera"], key="source_type", horizontal=True)

            video_source = None
            source_mode = "webcam"
            webcam_index = 0

            if source_type == "Webcam":
                webcam_index = st.number_input("Webcam Index", min_value=0, max_value=5, value=cam_config.WEBCAM_ID)
                video_source = int(webcam_index)
                source_mode = "webcam"

            elif source_type == "Video File":
                test_videos_dir = REPO_ROOT / "vids"
                available_videos = sorted([
                    f for f in test_videos_dir.glob("*")
                    if f.suffix.lower() in ['.mp4', '.avi', '.mov', '.mkv']
                ]) if test_videos_dir.exists() else []

                upload_method = st.radio("Method", ["Browse", "Upload", "Path"], horizontal=True)
                if upload_method == "Browse":
                    if available_videos:
                        video_options = {str(v): v.name for v in available_videos}
                        selected_video = st.selectbox("Video", options=list(video_options.keys()), format_func=lambda x: video_options[x])
                        if selected_video:
                            video_source = selected_video
                    else:
                        st.caption("No videos in vids/")
                elif upload_method == "Upload":
                    video_file = st.file_uploader("Upload (max 200MB)", type=['mp4', 'avi', 'mov'])
                    if video_file:
                        temp_path = REPO_ROOT / "temp" / video_file.name
                        temp_path.parent.mkdir(parents=True, exist_ok=True)
                        with open(temp_path, 'wb') as f:
                            f.write(video_file.read())
                        video_source = str(temp_path)
                else:
                    default_path = cam_config.VIDEO_FILE_PATH if Path(cam_config.VIDEO_FILE_PATH).exists() else ""
                    file_path_input = st.text_input("File Path", value=default_path, placeholder="C:/Videos/my.mp4")
                    if file_path_input:
                        file_path = Path(file_path_input)
                        if not file_path.is_absolute():
                            file_path = REPO_ROOT / file_path_input
                        if file_path.exists():
                            video_source = str(file_path)
                        else:
                            st.error(f"Not found: {file_path}")
                source_mode = "video"

            else:  # RTSP
                rtsp_cameras = cam_config.get_all_rtsp_cameras()
                camera_options = {key: f"{key} - {name}" for key, name, enabled in rtsp_cameras if enabled}
                if camera_options:
                    selected_camera = st.selectbox("Camera", options=list(camera_options.keys()), format_func=lambda x: camera_options[x])
                    video_source = cam_config.get_rtsp_url(selected_camera)
                else:
                    st.error("No RTSP cameras configured — add one below ↓")
                source_mode = "rtsp"

            # Dual camera (nested inside source)
            enable_dual_cam = st.checkbox("Dual Camera", value=False)
            secondary_source = None
            secondary_mode = None
            _sec_chose_rtsp = False
            if enable_dual_cam:
                sec_source_type = st.radio("Secondary", ["Webcam", "RTSP"], key="sec_source_type", horizontal=True)
                if sec_source_type == "Webcam":
                    sec_webcam_index = st.number_input("Sec. Webcam", min_value=0, max_value=5, value=cam_config.WEBCAM_ID_SECONDARY)
                    secondary_source = int(sec_webcam_index)
                    secondary_mode = "webcam"
                else:
                    _sec_chose_rtsp = True
                    rtsp_cameras = cam_config.get_all_rtsp_cameras()
                    sec_camera_options = {key: f"{key} - {name}" for key, name, enabled in rtsp_cameras if enabled}
                    if sec_camera_options:
                        sec_selected_camera = st.selectbox("Sec. RTSP", options=list(sec_camera_options.keys()), format_func=lambda x: sec_camera_options[x], key="sec_rtsp_select")
                        secondary_source = cam_config.get_rtsp_url(sec_selected_camera)
                        secondary_mode = "rtsp"
                    else:
                        st.error("No RTSP cameras configured — add one below ↓")

            # Always show manage button when RTSP is relevant (primary or secondary)
            _show_manage = (source_type == "RTSP Camera") or _sec_chose_rtsp
            if _show_manage:
                st.divider()
                _render_rtsp_camera_manager()

        # ===== 2. DETECTION SETTINGS =====
        with st.expander("🎯 Detection & Behavior", expanded=False):
            enable_behavior = st.checkbox("Enable HOI Detection", value=True, help="Detect human-object interactions")
            use_gpu = st.checkbox("Use GPU", value=False)
            device = "cuda" if use_gpu else "cpu"

            if enable_behavior:
                coverage_thresh = st.slider("Coverage Threshold", 0.05, 0.5, 0.18)
                move_px_thresh = st.slider("Movement Threshold (px)", 1.0, 20.0, 8.0)
                stationary_frames = st.slider("Stationary Frames", 3, 15, 6)
                status_on_frames = st.slider("Status ON Frames", 3, 15, 6)
                status_off_frames = st.slider("Status OFF Frames", 6, 30, 12)
                object_hold_frames = st.slider("Object Hold Frames", 3, 20, 8)
            else:
                coverage_thresh, move_px_thresh = 0.18, 8.0
                stationary_frames, status_on_frames = 6, 6
                status_off_frames, object_hold_frames = 12, 8

            st.divider()
            frame_skip = st.slider("Frame Skip", 1, 10, 1)
            resize_factor = st.slider("Resize Factor", 0.1, 1.0, 1.0)
            recog_interval = st.slider("Recognition Interval", 10, 60, 30)

        # ===== 3. PERSONNEL MANAGEMENT =====
        with st.expander("👥 Personnel & Authorization", expanded=False):
            # Registration
            st.markdown('<p class="sidebar-section-label">Face Registration</p>', unsafe_allow_html=True)
            person_name = st.text_input("Person Name", placeholder="Enter name", key="reg_person_name")
            col_cap1, col_cap2 = st.columns(2)
            with col_cap1:
                num_images = st.number_input("Images", min_value=20, max_value=200, value=100)
            with col_cap2:
                capture_webcam_id = st.number_input("Cam ID", min_value=0, max_value=5, value=0)

            if st.button("📸 Capture Faces", use_container_width=True, disabled=not person_name):
                if person_name and person_name.strip():
                    try:
                        import subprocess
                        capture_script = REPO_ROOT / "face_recognition" / "Facenet" / "facenet_capture.py"
                        if capture_script.exists():
                            with st.spinner("Capturing..."):
                                result = subprocess.run(
                                    [sys.executable, str(train_script)],
                                    capture_output=True, text=True, cwd=str(REPO_ROOT)
                                )
                                if result.returncode == 0:
                                    st.success(f"✅ Captured for {person_name}")
                                else:
                                    st.error(f"Failed: {result.stderr}")
                    except Exception as e:
                        st.error(str(e))

            if st.button("🚀 Train Model", use_container_width=True, type="primary"):
                try:
                    import subprocess
                    train_script = REPO_ROOT / "face_recognition" / "Facenet" / "facenet_train.py"
                    if train_script.exists():
                        with st.spinner("Training..."):
                            result = subprocess.run(
                                [sys.executable, str(train_script)],
                                capture_output=True, text=True, cwd=str(REPO_ROOT)
                            )
                            if result.returncode == 0:
                                st.success("✅ Training complete!")
                                st.balloons()
                            else:
                                st.error(result.stderr)
                except Exception as e:
                    st.error(str(e))

            st.divider()

            # Authorization management
            st.markdown('<p class="sidebar-section-label">Authorization Levels</p>', unsafe_allow_html=True)
            if st.button("� Refresh List", use_container_width=True):
                st.session_state.auth_manager.refresh()
                st.rerun()

            current_map = st.session_state.auth_manager.get_all_authorizations()
            if current_map:
                for pname in sorted(current_map.keys()):
                    current_level = current_map[pname]
                    new_level = st.selectbox(
                        f"👤 {pname}",
                        ["Authorized", "Partially Authorized", "Unauthorized"],
                        index=["Authorized", "Partially Authorized", "Unauthorized"].index(current_level),
                        key=f"auth_{pname}"
                    )
                    if new_level != current_level:
                        if st.button(f"Save {pname}", key=f"save_{pname}", use_container_width=True):
                            st.session_state.auth_manager.set_authorization(pname, new_level)
                            st.rerun()
            else:
                st.caption("No personnel registered yet.")

        # ===== 4. ADVANCED (rarely changed) =====
        with st.expander("⚙️ Advanced", expanded=False):
            yolo_model = st.text_input("YOLO Model", value="models/YOLOv11/yolo11n.pt")
            facenet_main = st.text_input("FaceNet Main", value=str(REPO_ROOT / "face_recognition" / "Facenet" / "facenet_main.py"))
            st.session_state.alert_sounds_enabled = st.checkbox("Alert Sounds", value=True)
    
    # Handle start/stop
    if start_button and not st.session_state.running:
        if video_source is not None:
            st.session_state.running = True
            st.session_state.stop_flag.clear()
            st.session_state.recording_enabled = enable_recording
            
            config = {
                'yolo_model': yolo_model,
                'facenet_main': facenet_main,
                'device': device,
                'recog_interval': recog_interval,
                'frame_skip': frame_skip,
                'resize_factor': resize_factor,
                'enable_logging': enable_logging,
                'webcam_index': int(webcam_index) if 'webcam_index' in locals() else 0,
                'conf_threshold': 0.5,
                'iou_threshold': 0.7,
                'source_mode': source_mode,
                'recording_enabled': enable_recording,
                'enable_behavior': enable_behavior,
                'coverage_thresh': coverage_thresh,
                'move_px_thresh': move_px_thresh,
                'stationary_frames_required': stationary_frames,
                'status_on_frames_required': status_on_frames,
                'status_off_frames_required': status_off_frames,
                'object_hold_frames': object_hold_frames,
                # Dual camera configuration
                'enable_dual_cam': enable_dual_cam if 'enable_dual_cam' in locals() else False,
                'secondary_source': secondary_source if 'secondary_source' in locals() else None,
                'secondary_mode': secondary_mode if 'secondary_mode' in locals() else None,
                # Pass authorization map with lowercase keys
                'authorization_map': {k.lower(): v for k, v in st.session_state.auth_manager.get_all_authorizations().items()},
            }
            
            # Clear queues
            while not st.session_state.frame_queue.empty():
                try:
                    st.session_state.frame_queue.get_nowait()
                except:
                    break
            
            while not st.session_state.log_queue.empty():
                try:
                    st.session_state.log_queue.get_nowait()
                except:
                    break
            
            # Clear tracking sets for new session
            st.session_state.all_logs = []
            st.session_state.last_eval_report = None
            st.session_state.session_metrics = None
            
            # Start processing thread with Streamlit context
            thread = threading.Thread(
                target=video_processing_thread,
                args=(video_source, config, st.session_state.frame_queue, st.session_state.log_queue, st.session_state.stop_flag),
                daemon=True
            )
            # Add Streamlit context to the thread to prevent warnings
            add_script_run_ctx(thread)
            thread.start()
            st.session_state.processing_thread = thread
            
            time.sleep(0.1)
            st.rerun()
        else:
            st.error("❌ Select a video source first")
    
    if stop_button and st.session_state.running:
        st.session_state.running = False
        st.session_state.stop_flag.set()
        time.sleep(0.1)
        st.rerun()

    # Main content area
    col1, col2 = st.columns([2, 1])

    with col1:
        st.subheader("📹 Live Feed")
        status_placeholder = st.empty()
        video_placeholder = st.empty()
        alert_placeholder = st.empty()

    with col2:
        st.subheader("📊 Overview")
        stats_placeholder = st.empty()
        metrics_placeholder = st.empty()   # Live FPS / Detection / Prediction rates
        st.subheader("🔍 Detections")
        detections_placeholder = st.empty()

    # Always show log section in UI
    st.subheader("📋 System Log")
    
    # "View All Logs" toggle + "Open Logs Folder" buttons
    col_log1, col_log2, col_log3 = st.columns([2, 1, 1])
    with col_log2:
        if st.button("📜 View All Logs", use_container_width=True):
            st.session_state.show_all_logs = not st.session_state.get("show_all_logs", False)
    with col_log3:
        if st.button("📂 Open Logs Folder", use_container_width=True):
            import subprocess, platform
            logs_dir = REPO_ROOT / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            try:
                {"Windows": lambda p: subprocess.run(["explorer", str(p)]),
                 "Darwin": lambda p: subprocess.run(["open", str(p)])
                }.get(platform.system(), lambda p: subprocess.run(["xdg-open", str(p)]))(logs_dir)
            except Exception:
                pass
    
    log_placeholder = st.empty()
    
    # ---------- Searchable, date-filtered log viewer ----------
    if st.session_state.get("show_all_logs", False):
        with st.expander("📋 Activity Log Viewer", expanded=True):
            _render_activity_log_viewer()

    # Placeholder for eval report — filled after queue drain in the stopped branch
    eval_report_slot = st.empty()

    # Display loop
    if st.session_state.running:
        rec_label = " · 🔴 REC" if st.session_state.recording_enabled else ""
        status_placeholder.markdown(
            f'<div style="padding:0.4rem 0.8rem;border-radius:6px;background:rgba(40,167,69,0.15);'
            f'color:#6fcf7f;font-weight:600;font-size:0.85rem;display:inline-block;">'
            f'🟢 Running{rec_label}</div>',
            unsafe_allow_html=True,
        )
        
        while st.session_state.running:
            # Get frame
            try:
                if not st.session_state.frame_queue.empty():
                    frame = st.session_state.frame_queue.get_nowait()
                    frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    video_placeholder.image(frame_rgb, channels="RGB", width=640)
                else:
                    time.sleep(0.01)
                    continue
            except queue.Empty:
                time.sleep(0.01)
                continue
            except Exception as e:
                if "MediaFileStorageError" not in str(type(e).__name__):
                    print(f"Display error: {e}")
                time.sleep(0.01)
                continue
            
            # Update detections + stats
            try:
                total = len(st.session_state.current_detections)
                auth = sum(1 for d in st.session_state.current_detections if d['authorization'] == "Authorized")
                partial = sum(1 for d in st.session_state.current_detections if d['authorization'] == "Partially Authorized")
                unauth = sum(1 for d in st.session_state.current_detections if d['authorization'] == "Unauthorized")

                # Stat badges (custom HTML instead of st.metric)
                stats_placeholder.markdown(f"""
                <div class="stat-row">
                    <div class="stat-badge stat-total">
                        <div class="stat-label">Total</div>{total}
                    </div>
                    <div class="stat-badge stat-auth">
                        <div class="stat-label">Auth</div>{auth}
                    </div>
                    <div class="stat-badge" style="background:rgba(255,193,7,0.15);color:#ffd95c;">
                        <div class="stat-label">Partial</div>{partial}
                    </div>
                    <div class="stat-badge stat-unauth">
                        <div class="stat-label">Unauth</div>{unauth}
                    </div>
                </div>
                """, unsafe_allow_html=True)

                # --- Live evaluation metrics ---
                _m = st.session_state.get("session_metrics")
                if _m is not None:
                    try:
                        _snap = _m.snapshot()
                        metrics_placeholder.markdown(f"""
                        <div class="eval-bar">
                            <div class="eval-chip eval-fps">
                                <div class="eval-label">FPS</div>{_snap['fps']}
                            </div>
                            <div class="eval-chip eval-det">
                                <div class="eval-label">Detection</div>{_snap['detection_rate']}%
                            </div>
                            <div class="eval-chip eval-pred">
                                <div class="eval-label">Prediction</div>{_snap['prediction_rate']}%
                            </div>
                            <div class="eval-chip eval-ppl">
                                <div class="eval-label">People</div>{_snap['unique_people']}
                            </div>
                        </div>
                        """, unsafe_allow_html=True)
                    except Exception:
                        pass

                # Detection cards
                with detections_placeholder.container():
                    if st.session_state.current_detections:
                        for detection in st.session_state.current_detections:
                            auth_cls = "det-authorized" if detection['authorization'] == "Authorized" else \
                                       "det-partial" if detection['authorization'] == "Partially Authorized" else "det-unauthorized"

                            behavior_text = detection.get('behavior_status', '')
                            behavior_html = f'<div class="det-behavior">{behavior_text}</div>' if behavior_text else ''
                            camera_tag = detection.get('camera', 'Primary')

                            st.markdown(f"""
                            <div class="det-card {auth_cls}">
                                <div class="det-name">{detection['identity']}</div>
                                <div class="det-meta">📷 {camera_tag} · {detection['authorization']}</div>
                                {behavior_html}
                            </div>
                            """, unsafe_allow_html=True)

                            # Generate alerts for unauthorized/partial
                            if detection['authorization'] in ["Unauthorized", "Partially Authorized"]:
                                show_alert(detection['authorization'], detection['identity'])
                    else:
                        st.info("No detections")

                # Display all active alerts (with expiration)
                active_alerts_html = get_active_alerts()
                if active_alerts_html:
                    alert_placeholder.markdown(active_alerts_html, unsafe_allow_html=True)
                else:
                    alert_placeholder.empty()

            except:
                pass
            
            # Update logs
            log_messages = []
            while not st.session_state.log_queue.empty():
                try:
                    msg = st.session_state.log_queue.get_nowait()
                    if msg.get('type') == 'detections':
                        st.session_state.current_detections = msg.get('data', [])
                    elif msg.get('type') == 'recording_path':
                        st.session_state.recording_path = msg.get('path')
                    elif msg.get('type') == 'log_file_path':
                        st.session_state.session_log_file = msg.get('path')
                    elif msg.get('type') == 'eval_report':
                        st.session_state.last_eval_report = msg.get('data')
                    else:
                        # Add timestamp if not present
                        if 'timestamp' not in msg:
                            msg['timestamp'] = datetime.now().strftime("%H:%M:%S")
                        
                        # Store in all_logs for history
                        st.session_state.all_logs.append(msg)
                        log_messages.append(msg)
                except queue.Empty:
                    break
            
            # Always display logs in UI (even if logging is disabled in backend)
            if log_messages:
                try:
                    with log_placeholder.container():
                        for msg in log_messages[-5:]:
                            msg_type = msg.get('type', 'info')
                            message = msg.get('message', '')
                            timestamp = msg.get('timestamp', '')
                            
                            if msg_type == 'error':
                                st.error(f"[{timestamp}] ❌ {message}")
                            elif msg_type == 'warning':
                                st.warning(f"[{timestamp}] ⚠️ {message}")
                            elif msg_type == 'success':
                                st.success(f"[{timestamp}] ✅ {message}")
                            else:
                                st.info(f"[{timestamp}] ℹ️ {message}")
                except:
                    pass
            
            time.sleep(0.01)
            
            if not st.session_state.running:
                break

        # --- Drain any remaining queue messages (catches eval_report sent during cleanup) ---
        _drain_remaining_queue()

    else:
        # --- Wait briefly for processing thread to finish cleanup & post eval_report ---
        _thread = st.session_state.get("processing_thread")
        if _thread and _thread.is_alive():
            _thread.join(timeout=3.0)  # wait up to 3s for cleanup

        # --- Drain queue on stopped screen (catches eval_report sent during cleanup) ---
        _drain_remaining_queue()

        # --- Render evaluation report now that queue is drained ---
        if st.session_state.last_eval_report:
            with eval_report_slot.expander("📊 System Evaluation Report", expanded=True):
                _render_eval_report(st.session_state.last_eval_report)

        status_placeholder.markdown(
            '<div style="padding:0.4rem 0.8rem;border-radius:6px;background:rgba(220,53,69,0.15);'
            'color:#ff7a85;font-weight:600;font-size:0.85rem;display:inline-block;">'
            '🔴 Stopped</div>',
            unsafe_allow_html=True,
        )
        video_placeholder.empty()
        
        # Show download button for last recording
        if st.session_state.recording_path and Path(st.session_state.recording_path).exists():
            st.divider()
            st.subheader("📥 Last Recording")
            
            recording_file = Path(st.session_state.recording_path)
            file_size_mb = recording_file.stat().st_size / (1024 * 1024)
            
            st.info(f"**{recording_file.name}** ({file_size_mb:.1f} MB)")
            
            with open(recording_file, 'rb') as f:
                st.download_button(
                    label="⬇️ Download Recording",
                    data=f.read(),
                    file_name=recording_file.name,
                    mime="video/mp4",
                    use_container_width=True
                )

if __name__ == "__main__":
    main()
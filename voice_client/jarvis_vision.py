"""자비스 비전 데몬 — 카메라로 보고 알아본다.

음성 데몬과 같은 자리(사용자 PC)에서 따로 도는 프로세스다. 카메라를 붙들고 있다가
서버가 "한 장 줘" 하면 그때 보낸다. 평소엔 아무것도 올리지 않는다.

얼굴 매칭은 여기서 끝난다. 서버에는 등록된 얼굴의 임베딩만 받아 오고, 사진은
한 장도 보내지 않는다 — 아이 얼굴 같은 걸 클라우드에 두지 않기 위해서다.
클라우드로 나가는 건 "이거 뭐야" 하고 물었을 때의 프레임 한 장뿐이다.

    python voice_client/jarvis_vision.py              # 상시 실행
    python voice_client/jarvis_vision.py --test       # 미리보기 창으로 인식 확인
    python voice_client/jarvis_vision.py --enroll 창래 --relation 본인
    python voice_client/jarvis_vision.py --list
    python voice_client/jarvis_vision.py --forget 창래

얼굴 인식은 OpenCV 의 YuNet(검출) + SFace(특징) 를 쓴다. dlib 이나 insightface 와 달리
pip 만으로 윈도우에 깔리고 모델도 작다(합쳐 40MB 남짓). 제스처는 mediapipe 가 있을
때만 켜진다 — 없어도 나머지는 그대로 돈다.
"""

import argparse
import base64
import json
import os
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import requests
import websocket  # websocket-client

BASE_DIR = Path(__file__).resolve().parent
MODEL_DIR = BASE_DIR / "models"


def _load_dotenv() -> None:
    """음성 데몬과 같은 .env 를 읽는다. 키를 두 군데 적지 않으려고."""
    path = BASE_DIR.parent / ".env"
    if not path.exists():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    except Exception as exc:
        print("(.env 로딩 실패, 무시)", exc)


_load_dotenv()

BACKEND = os.environ.get("JAVIS_BACKEND", "http://localhost:8000")
WS_URL = BACKEND.replace("https", "wss").replace("http", "ws") + "/ws/vision"
TOKEN = os.environ.get("JAVIS_TOKEN", "").strip()
AUTH_HEADERS = {"X-Javis-Token": TOKEN} if TOKEN else {}

CAMERA_INDEX = int(os.environ.get("JAVIS_CAMERA", "0"))
# 얼굴을 몇 초마다 확인할지. 매 프레임 돌릴 이유가 없다 — 사람은 그렇게 빨리 안 바뀐다.
FACE_INTERVAL = float(os.environ.get("JAVIS_FACE_INTERVAL", "1.0"))
# SFace 코사인 유사도 임계값. OpenCV 권장값이 0.363 이다. 높일수록 엄격(오인식↓, 미인식↑).
FACE_THRESHOLD = float(os.environ.get("JAVIS_FACE_THRESHOLD", "0.363"))
DETECT_SCORE = float(os.environ.get("JAVIS_FACE_SCORE", "0.8"))
JPEG_QUALITY = int(os.environ.get("JAVIS_JPEG_QUALITY", "85"))

MODELS = {
    "detector": (
        "face_detection_yunet_2023mar.onnx",
        "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/"
        "models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    ),
    "recognizer": (
        "face_recognition_sface_2021dec.onnx",
        "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/"
        "models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
    ),
}


# --- 모델 준비 ---


def ensure_model(kind: str) -> Path:
    name, url = MODELS[kind]
    path = MODEL_DIR / name
    if path.exists() and path.stat().st_size > 100_000:
        return path

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    print(f"모델 내려받는 중: {name}")
    tmp = path.with_suffix(".part")
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with tmp.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                f.write(chunk)

    # opencv_zoo 는 git-lfs 를 쓴다. 주소가 잘못되면 모델 대신 포인터 텍스트가 내려온다.
    head = tmp.open("rb").read(64)
    if head.startswith(b"version https://git-lfs"):
        tmp.unlink(missing_ok=True)
        raise SystemExit(
            f"{name} 대신 git-lfs 포인터가 내려왔습니다. "
            f"브라우저로 아래 주소에서 직접 받아 {MODEL_DIR} 에 넣어 주세요.\n{url}"
        )

    tmp.replace(path)
    print(f"완료: {path} ({path.stat().st_size // 1024}KB)")
    return path


# --- 얼굴 ---


class FaceEngine:
    """검출(YuNet) + 특징 추출(SFace) + 등록 얼굴과 매칭."""

    def __init__(self) -> None:
        self.detector = cv2.FaceDetectorYN.create(
            str(ensure_model("detector")), "", (320, 320), DETECT_SCORE, 0.3, 5000
        )
        self.recognizer = cv2.FaceRecognizerSF.create(str(ensure_model("recognizer")), "")
        self.known: list[tuple[str, np.ndarray]] = []
        self._size: tuple[int, int] | None = None

    def load_known(self) -> str:
        """등록된 얼굴을 서버에서 받아 온다. 임베딩만 오고 사진은 오지 않는다."""
        try:
            res = requests.get(f"{BACKEND}/vision/faces", headers=AUTH_HEADERS, timeout=10)
            res.raise_for_status()
            rows = res.json()
        except Exception as exc:
            return f"등록된 얼굴을 받아오지 못했습니다: {exc}"

        self.known = [
            (row["name"], _normalize(np.array(row["embedding"], dtype=np.float32)))
            for row in rows
            if row.get("embedding")
        ]
        return f"등록된 얼굴 {len(self.known)}명 불러옴"

    def detect(self, frame) -> list:
        h, w = frame.shape[:2]
        if self._size != (w, h):
            self.detector.setInputSize((w, h))
            self._size = (w, h)
        _, faces = self.detector.detect(frame)
        return [] if faces is None else list(faces)

    def embed(self, frame, face) -> np.ndarray:
        aligned = self.recognizer.alignCrop(frame, face)
        return _normalize(self.recognizer.feature(aligned).flatten().astype(np.float32))

    def identify(self, frame) -> tuple[list[str], int, list]:
        """(알아본 이름들, 모르는 얼굴 수, 검출 상자들)."""
        faces = self.detect(frame)
        names: list[str] = []
        unknown = 0

        for face in faces:
            try:
                vec = self.embed(frame, face)
            except cv2.error:
                continue
            name, score = self.best_match(vec)
            if name is None:
                unknown += 1
            elif name not in names:
                names.append(name)

        return names, unknown, faces

    def best_match(self, vec: np.ndarray) -> tuple[str | None, float]:
        best_name, best_score = None, -1.0
        for name, known in self.known:
            score = float(np.dot(vec, known))  # 둘 다 정규화돼 있어 내적 = 코사인
            if score > best_score:
                best_name, best_score = name, score
        if best_score < FACE_THRESHOLD:
            return None, best_score
        return best_name, best_score


def _normalize(vec: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm > 1e-9 else vec


# --- 제스처 (mediapipe 가 있을 때만) ---


def classify_motion(dx: float, dy: float, move: float) -> str | None:
    """두 손이 어떻게 움직였는지 판정한다.

    변화가 큰 축을 먼저 고른다. 안 그러면 손을 비스듬히 움직일 때 두 방향이 같이
    걸려 엉뚱한 게 나간다.
    """
    if abs(dx) > abs(dy):
        return ("spread" if dx > 0 else "gather") if abs(dx) > move else None
    return ("push_down" if dy > 0 else "pull_up") if abs(dy) > move else None


class GestureEngine:
    """두 손 모션만 본다.

    한 손 포즈(손바닥 펴기·주먹)는 손을 들기만 해도 오발동한다 — 카메라 앞에서
    자연스럽게 움직이다 보면 계속 튄다. 두 손을 '움직여야' 발동하는 방식이
    실제로 쓸 만하다.
    """

    HISTORY = 10  # 약 0.33초
    MOVE = 0.20  # 정규화 좌표 기준 변화량
    COOLDOWN = 1.0

    def __init__(self) -> None:
        import mediapipe as mp

        self._mp = mp
        self.hands = mp.solutions.hands.Hands(
            max_num_hands=2, min_detection_confidence=0.6, min_tracking_confidence=0.5
        )
        self.gap: list[float] = []
        self.height: list[float] = []
        self.last_fired = 0.0

    @staticmethod
    def available() -> bool:
        try:
            import mediapipe  # noqa: F401
        except Exception:
            return False
        return True

    def feed(self, frame) -> str | None:
        result = self.hands.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        marks = result.multi_hand_landmarks or []
        if len(marks) != 2:
            self.gap.clear()
            self.height.clear()
            return None

        wrists = [hand.landmark[0] for hand in marks]
        self.gap.append(abs(wrists[0].x - wrists[1].x))
        self.height.append((wrists[0].y + wrists[1].y) / 2)
        if len(self.gap) < self.HISTORY:
            return None
        self.gap = self.gap[-self.HISTORY :]
        self.height = self.height[-self.HISTORY :]

        now = time.time()
        if now - self.last_fired < self.COOLDOWN:
            return None

        name = classify_motion(
            self.gap[-1] - self.gap[0], self.height[-1] - self.height[0], self.MOVE
        )
        if name:
            self.last_fired = now
            self.gap.clear()
            self.height.clear()
        return name


# 제스처 → 윈도우 미디어 키. --control 을 줬을 때만 실제로 누른다.
_MEDIA_KEYS = {
    "spread": 0xB0,  # 다음 트랙
    "gather": 0xB1,  # 이전 트랙
    "pull_up": 0xB3,  # 재생/일시정지
    "push_down": 0xAD,  # 음소거
}


def press_media(gesture: str) -> None:
    key = _MEDIA_KEYS.get(gesture)
    if key is None:
        return
    try:
        import win32api
        import win32con

        win32api.keybd_event(key, 0, 0, 0)
        win32api.keybd_event(key, 0, win32con.KEYEVENTF_KEYUP, 0)
    except Exception as exc:
        print("  (미디어 키 실패)", exc)


# --- 데몬 ---


class VisionDaemon:
    def __init__(self, *, control: bool = False, preview: bool = False) -> None:
        self.faces = FaceEngine()
        self.gestures = GestureEngine() if GestureEngine.available() else None
        self.control = control
        self.preview = preview

        self.frame = None
        self.boxes: list = []
        self.frame_lock = threading.Lock()
        self.send_lock = threading.Lock()
        self.ws: websocket.WebSocket | None = None
        self.running = True
        self.last_state: tuple[tuple[str, ...], int] = ((), 0)

    # --- 통신 ---

    def send(self, payload: dict) -> None:
        with self.send_lock:
            ws = self.ws
            if ws is None:
                return
            try:
                ws.send(json.dumps(payload))
            except Exception:
                pass  # 재연결 루프가 알아서 다시 붙는다

    def hud(self, state: str, text: str = "") -> None:
        try:
            requests.post(
                f"{BACKEND}/hud/event",
                json={"state": state, "text": text},
                headers=AUTH_HEADERS,
                timeout=2,
            )
        except Exception:
            pass

    def _send_frame(self) -> None:
        with self.frame_lock:
            frame = None if self.frame is None else self.frame.copy()
        if frame is None:
            self.send({"type": "error", "message": "카메라에서 아직 화면을 받지 못했습니다."})
            return

        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
        if not ok:
            self.send({"type": "error", "message": "화면을 인코딩하지 못했습니다."})
            return
        self.send({"type": "frame", "image": base64.b64encode(buf.tobytes()).decode("ascii")})

    # --- 카메라 ---

    def _camera_loop(self) -> None:
        cap = _open_camera()
        if cap is None:
            print("카메라를 열지 못했습니다. JAVIS_CAMERA 로 다른 번호를 지정해 보세요.")
            self.running = False
            return

        last_check = 0.0
        try:
            while self.running:
                ok, frame = cap.read()
                if not ok:
                    time.sleep(0.2)
                    continue

                with self.frame_lock:
                    self.frame = frame

                now = time.time()
                if now - last_check >= FACE_INTERVAL:
                    last_check = now
                    self._check_faces(frame)

                if self.gestures and (gesture := self.gestures.feed(frame)):
                    print(f"제스처: {gesture}")
                    self.send({"type": "gesture", "name": gesture})
                    if self.control:
                        press_media(gesture)

                if self.preview and not self._draw(frame):
                    self.running = False
        finally:
            cap.release()
            if self.preview:
                cv2.destroyAllWindows()

    def _check_faces(self, frame) -> None:
        try:
            names, unknown, boxes = self.faces.identify(frame)
        except cv2.error as exc:
            print("  (얼굴 인식 실패)", exc)
            return

        self.boxes = boxes
        state = (tuple(names), unknown)
        if state == self.last_state:
            return  # 바뀐 게 없으면 보내지 않는다
        self.last_state = state

        self.send({"type": "faces", "names": names, "unknown": unknown})
        if names:
            print(f"보임: {', '.join(names)}" + (f" (+모르는 사람 {unknown})" if unknown else ""))

    def _draw(self, frame) -> bool:
        """--test 용 미리보기. 창을 닫거나 q 를 누르면 False."""
        shown = frame.copy()
        for face in self.boxes:
            x, y, w, h = (int(v) for v in face[:4])
            cv2.rectangle(shown, (x, y), (x + w, y + h), (0, 220, 255), 2)
        label = ", ".join(self.last_state[0]) or "(모르는 얼굴)" if self.boxes else ""
        if label:
            cv2.putText(shown, label, (12, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 220, 255), 2)
        cv2.imshow("javis vision", shown)
        return cv2.waitKey(1) & 0xFF != ord("q")

    # --- 메인 루프 ---

    def run(self) -> None:
        print(self.faces.load_known())
        print(f"제스처: {'켜짐' if self.gestures else '꺼짐 (mediapipe 없음)'}")

        camera = threading.Thread(target=self._camera_loop, daemon=True)
        camera.start()

        while self.running:
            try:
                ws = websocket.create_connection(WS_URL, timeout=60)
                if TOKEN:
                    ws.send(json.dumps({"token": TOKEN}))
                self.ws = ws
                print(f"서버 연결됨: {WS_URL}")
                self.hud("vision", "카메라 준비됨")

                while self.running:
                    try:
                        message = json.loads(ws.recv())
                    except websocket.WebSocketTimeoutException:
                        continue  # 조용한 시간. 연결은 살아 있다.
                    if (message or {}).get("type") == "capture":
                        self._send_frame()
            except KeyboardInterrupt:
                self.running = False
            except Exception as exc:
                if self.running:
                    print(f"서버 연결 끊김({exc}). 3초 뒤 재연결…")
                    time.sleep(3)
            finally:
                with self.send_lock:
                    self.ws = None

        camera.join(timeout=3)


def _open_camera():
    # 윈도우 기본 백엔드(MSMF)는 첫 프레임까지 몇 초씩 걸리는 경우가 있다. DSHOW 가 빠르다.
    backends = [cv2.CAP_DSHOW, cv2.CAP_ANY] if sys.platform == "win32" else [cv2.CAP_ANY]
    for backend in backends:
        cap = cv2.VideoCapture(CAMERA_INDEX, backend)
        if cap.isOpened():
            return cap
        cap.release()
    return None


# --- 얼굴 등록 ---


def enroll(name: str, relation: str, samples: int = 12) -> int:
    """여러 프레임의 임베딩을 평균 내 등록한다. 한 장만 쓰면 각도 하나에만 맞는다."""
    engine = FaceEngine()
    cap = _open_camera()
    if cap is None:
        print("카메라를 열지 못했습니다.")
        return 1

    print(f"'{name}' 등록을 시작합니다. 카메라를 보고 고개를 천천히 좌우로 움직여 주세요.")
    collected: list[np.ndarray] = []
    try:
        deadline = time.time() + 30
        while len(collected) < samples and time.time() < deadline:
            ok, frame = cap.read()
            if not ok:
                continue
            faces = engine.detect(frame)
            if len(faces) != 1:
                # 둘 이상이면 누구를 등록하는지 알 수 없다.
                continue
            try:
                collected.append(engine.embed(frame, faces[0]))
            except cv2.error:
                continue
            print(f"  {len(collected)}/{samples}")
            time.sleep(0.25)
    finally:
        cap.release()

    if len(collected) < 3:
        print("얼굴을 충분히 잡지 못했습니다. 밝은 곳에서 다시 시도해 주세요.")
        return 1

    vector = _normalize(np.mean(collected, axis=0))
    try:
        res = requests.post(
            f"{BACKEND}/vision/face",
            json={"name": name, "relation": relation, "embedding": vector.tolist()},
            headers=AUTH_HEADERS,
            timeout=15,
        )
        res.raise_for_status()
    except Exception as exc:
        detail = getattr(getattr(exc, "response", None), "text", "")
        print(f"등록 실패: {exc} {detail[:200]}")
        return 1

    print(f"'{name}' 등록 완료 ({len(collected)}장 평균).")
    return 0


def list_faces() -> int:
    try:
        res = requests.get(f"{BACKEND}/vision/faces", headers=AUTH_HEADERS, timeout=10)
        res.raise_for_status()
    except Exception as exc:
        print(f"조회 실패: {exc}")
        return 1
    rows = res.json()
    if not rows:
        print("등록된 얼굴이 없습니다.")
        return 0
    for row in rows:
        print(f"- {row['name']}" + (f" ({row['relation']})" if row.get("relation") else ""))
    return 0


def forget_face(name: str) -> int:
    try:
        res = requests.delete(f"{BACKEND}/vision/face/{name}", headers=AUTH_HEADERS, timeout=10)
        res.raise_for_status()
    except Exception as exc:
        print(f"삭제 실패: {exc}")
        return 1
    print(f"'{name}' 을(를) 지웠습니다.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="자비스 비전 데몬")
    parser.add_argument("--test", action="store_true", help="미리보기 창을 띄운다")
    parser.add_argument("--control", action="store_true", help="제스처로 미디어 키를 누른다")
    parser.add_argument("--enroll", metavar="이름", help="얼굴을 등록한다")
    parser.add_argument("--relation", default="", help="--enroll 과 함께. 예: 본인, 아들")
    parser.add_argument("--list", action="store_true", help="등록된 얼굴 목록")
    parser.add_argument("--forget", metavar="이름", help="등록된 얼굴을 지운다")
    args = parser.parse_args()

    if args.list:
        return list_faces()
    if args.forget:
        return forget_face(args.forget)
    if args.enroll:
        return enroll(args.enroll, args.relation)

    daemon = VisionDaemon(control=args.control, preview=args.test)
    try:
        daemon.run()
    except KeyboardInterrupt:
        daemon.running = False
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

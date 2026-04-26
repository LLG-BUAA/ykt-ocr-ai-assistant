import base64
import copy
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from tkinter import BOTH, Canvas, DISABLED, END, LEFT, NORMAL, RIGHT, BooleanVar, StringVar, Text, Tk, filedialog, ttk

from PIL import Image, ImageChops, ImageDraw, ImageGrab, ImageStat, ImageTk

os.environ.setdefault("GLOG_minloglevel", "2")
os.environ.setdefault("FLAGS_minloglevel", "2")

from paddleocr import PaddleOCR

from app import (
    APP_DIR,
    OcrVisualizerWindow,
    Region,
    RegionPicker,
    cursor_position,
    enable_dpi_awareness,
    load_dotenv,
    virtual_screen_origin,
    virtual_screen_bounds,
)


RUNTIME_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else APP_DIR
CONFIG_PATH = RUNTIME_DIR / "config_paddle.json"
PROJECT_CONFIG_PATH = APP_DIR / "config_paddle.json"
EXAMPLE_CONFIG_PATH = APP_DIR / "config_paddle.example.json"
RUNTIME_DATA_DIR = RUNTIME_DIR / "runtime"
SNAPSHOT_DIR = RUNTIME_DATA_DIR / "snapshots"
RESULTS_PATH = RUNTIME_DATA_DIR / "results.jsonl"
DEFAULT_GEMINI_BASE_URL = "http://192.168.31.114:7999/v1beta"
DEFAULT_OPENAI_BASE_URL = "http://192.168.31.114:7999/v1"
DEFAULT_GEMINI_MODEL = "gemini-3-pro-preview"
DEFAULT_OPENAI_MODEL = "gpt-5.4"
DEFAULT_PARSE_REGEX = r"正确答案[:：]\s*([^\n\r]+)"
API_PROVIDER_OPTIONS = {"Gemini / Sub2API v1beta": "gemini", "OpenAI 兼容 / v1": "openai"}
API_PROVIDER_LABELS = {value: key for key, value in API_PROVIDER_OPTIONS.items()}
MODEL_PRESETS = (DEFAULT_GEMINI_MODEL, DEFAULT_OPENAI_MODEL)


ACTION_TYPES = {
    "ocr": "刷新 OCR",
    "if_text": "条件判断",
    "click_text": "点击文字",
    "wait": "等待",
    "ask_ai": "询问 AI",
    "parse_ai": "解析 AI",
    "click_matches": "点击解析选项",
}

ACTION_TYPE_OPTIONS = [f"{label} ({key})" for key, label in ACTION_TYPES.items()]
ACTION_LABEL_TO_KEY = {value: key for key, value in ACTION_TYPES.items()}
ACTION_OPTION_TO_KEY = {f"{label} ({key})": key for key, label in ACTION_TYPES.items()}
CONDITION_OPTIONS = {"文本包含": "text", "截图相似": "screenshot_similarity"}
CONDITION_LABELS = {value: key for key, value in CONDITION_OPTIONS.items()}
SOURCE_OPTIONS = {"OCR 文本": "ocr", "AI 回答": "ai"}
SOURCE_LABELS = {value: key for key, value in SOURCE_OPTIONS.items()}
MODE_OPTIONS = {"全部包含": "all", "任一包含": "any"}
MODE_LABELS = {value: key for key, value in MODE_OPTIONS.items()}
CLICK_AREA_OPTIONS = {"选项圆圈": "letter", "文字本身": "text"}
CLICK_AREA_LABELS = {value: key for key, value in CLICK_AREA_OPTIONS.items()}
BRANCH_OPTIONS = {"继续下一步": "continue", "跳过后面 N 步": "skip", "跳到第 N 步": "jump", "停止本轮": "stop"}
BRANCH_LABELS = {value: key for key, value in BRANCH_OPTIONS.items()}
ACTION_HINTS = {
    "ocr": "重新截取目标区域并运行 OCR。后续定位、条件判断和 AI 提问都会复用这次结果。",
    "if_text": "可以判断 OCR/AI 文本是否包含指定文字，也可以判断当前目标区域截图和前面第 N 张截图的前景内容是否相似；满足和不满足时都可以设置继续、跳过、跳转或停止。",
    "click_text": "在最近一次 OCR 结果中查找文字并点击。选择题里输入 A/B/C/D 或选项文字时，默认点左侧圆圈。",
    "wait": "暂停指定秒数，适合等待页面切换、动画或网络响应。",
    "ask_ai": "把提示语和可选截图发给 Sub2API/Gemini。可使用 {U}、{Q}、{OCR}、{AI}、{MATCHES} 等占位符。",
    "parse_ai": "用正则从 AI 回答或 OCR 文本中提取答案；支持 A-F、选项文字、多选文字和判断题的正确/错误。",
    "click_matches": "点击上一步解析得到的答案。多选会按顺序点击，文字答案会点击对应选项文字，判断题会点击对勾或叉号。",
}


def default_action_params(action_type: str) -> dict:
    defaults = {
        "ocr": {"force": True},
        "if_text": {
            "condition": "text",
            "text": "",
            "source": "ocr",
            "mode": "all",
            "similarity_threshold": "90",
            "similarity_lag": "1",
            "true_action": "continue",
            "true_value": "1",
            "false_action": "skip",
            "false_value": "1",
        },
        "click_text": {"text": "", "click_area": "letter"},
        "wait": {"seconds": "1"},
        "ask_ai": {"prompt": "{U}\n\nQ:\n{Q}", "include_image": False},
        "parse_ai": {"regex": DEFAULT_PARSE_REGEX, "source": "ai"},
        "click_matches": {"click_area": "text", "delay": "0.2"},
    }
    return dict(defaults.get(action_type, {}))


def default_workflow_actions() -> list[dict]:
    return [
        {"enabled": True, "type": "ocr", "params": {"force": True}},
        {"enabled": True, "type": "ask_ai", "params": {"prompt": "{U}\n\nQ:\n{Q}", "include_image": False}},
        {"enabled": True, "type": "parse_ai", "params": {"regex": DEFAULT_PARSE_REGEX}},
        {"enabled": False, "type": "click_matches", "params": {"click_area": "text", "delay": 0.2}},
    ]


def normalize_workflow_actions(actions) -> list[dict]:
    if not isinstance(actions, list) or not actions:
        actions = default_workflow_actions()
    normalized = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        action_type = action.get("type")
        if action_type not in ACTION_TYPES:
            continue
        params = action.get("params") if isinstance(action.get("params"), dict) else {}
        params = dict(params)
        if action_type == "if_text" and "skip_next" in params and "false_action" not in params:
            params["false_action"] = "skip"
            params["false_value"] = params.get("skip_next", "1")
        if action_type == "if_text":
            params.setdefault("condition", "text")
            params.setdefault("similarity_threshold", "90")
            params.setdefault("similarity_lag", "1")
        if action_type == "ask_ai":
            params["include_image"] = bool_value(params.get("include_image", False), False)
        normalized.append(
            {
                "enabled": bool(action.get("enabled", True)),
                "type": action_type,
                "params": params,
            }
        )
    return normalized or default_workflow_actions()


def bool_value(value, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if not text:
        return default
    return text not in {"0", "false", "no", "off", "否", "不", "关", "关闭"}


def parse_url_lines(value) -> list[str]:
    if isinstance(value, list):
        candidates = value
    else:
        candidates = str(value or "").splitlines()
    urls = []
    for raw in candidates:
        url = str(raw).strip()
        if not url or url.startswith("#"):
            continue
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
            url = "https://" + url
        urls.append(url)
    return urls


def browser_candidates() -> list[str]:
    names = ["msedge", "chrome", "msedge.exe", "chrome.exe"]
    paths = [path for name in names if (path := shutil.which(name))]
    program_files = [os.getenv("PROGRAMFILES", ""), os.getenv("PROGRAMFILES(X86)", ""), os.getenv("LOCALAPPDATA", "")]
    suffixes = [
        r"Microsoft\Edge\Application\msedge.exe",
        r"Google\Chrome\Application\chrome.exe",
    ]
    for root in program_files:
        if not root:
            continue
        for suffix in suffixes:
            paths.append(str(Path(root) / suffix))
    return list(dict.fromkeys(path for path in paths if path))


def detect_browser_path() -> str:
    for candidate in browser_candidates():
        if Path(candidate).exists():
            return candidate
    return ""


@dataclass
class PaddleAppConfig:
    interval_seconds: int = 3
    region: Region = field(default_factory=Region)
    prompt_u: str = "这个题选什么。直接告诉我答案。格式：正确答案：x,x,x,x 回答（ABCD，和正确答案的文本，遇到多选就用,隔开。如果是判断题就回答对错）"
    api_provider: str = "gemini"
    api_base_url: str = DEFAULT_GEMINI_BASE_URL
    gemini_model: str = DEFAULT_GEMINI_MODEL
    api_key_env: str = "SUB2API_API_KEY"
    api_key: str = ""
    paddle_lang: str = "ch"
    use_choice_formatter: bool = True
    save_snapshots: bool = True
    answer_validation_pause_on_mismatch: bool = False
    workflow_actions: list[dict] = field(default_factory=default_workflow_actions)
    browser_enabled: bool = False
    browser_path: str = ""
    browser_debug_port: int = 9223
    browser_window_x: int = 0
    browser_window_y: int = 0
    browser_window_w: int = 1200
    browser_window_h: int = 900
    browser_wait_seconds: float = 2
    browser_next_wait_seconds: float = 2
    browser_urls: list[str] = field(default_factory=list)


def load_config() -> PaddleAppConfig:
    load_dotenv(RUNTIME_DIR / ".env")
    if RUNTIME_DIR != APP_DIR:
        load_dotenv(APP_DIR / ".env")
    source_path = CONFIG_PATH if CONFIG_PATH.exists() else None
    if source_path is None and RUNTIME_DIR != APP_DIR and PROJECT_CONFIG_PATH.exists():
        source_path = PROJECT_CONFIG_PATH
    if source_path is None and EXAMPLE_CONFIG_PATH.exists():
        source_path = EXAMPLE_CONFIG_PATH
    if source_path is None:
        config = PaddleAppConfig()
        config.browser_path = detect_browser_path()
        return config
    raw = json.loads(source_path.read_text(encoding="utf-8"))
    raw["region"] = Region(**raw.get("region", {}))
    raw["workflow_actions"] = normalize_workflow_actions(raw.get("workflow_actions"))
    raw["browser_urls"] = parse_url_lines(raw.get("browser_urls", []))
    if "api_provider" not in raw:
        model = str(raw.get("gemini_model", "")).lower()
        base_url = str(raw.get("api_base_url", "")).rstrip("/")
        raw["api_provider"] = "openai" if base_url.endswith("/v1") or model.startswith("gpt-") else "gemini"
    config = PaddleAppConfig(**raw)
    if not config.browser_path or not Path(config.browser_path).exists():
        config.browser_path = detect_browser_path()
    if source_path != CONFIG_PATH:
        try:
            save_config(config)
        except Exception:
            pass
    return config


def save_config(config: PaddleAppConfig) -> None:
    CONFIG_PATH.write_text(json.dumps(asdict(config), ensure_ascii=False, indent=2), encoding="utf-8")


class BrowserController:
    def __init__(self, config: PaddleAppConfig):
        self.config = config
        self.process: subprocess.Popen | None = None
        self.current_tab_id = ""
        self.profile_tempdir: tempfile.TemporaryDirectory | None = None
        self.profile_dir: Path | None = None

    @property
    def cdp_base_url(self) -> str:
        return f"http://127.0.0.1:{int(self.config.browser_debug_port)}"

    def browser_path(self) -> str:
        path = self.config.browser_path.strip() or detect_browser_path()
        if not path or not Path(path).exists():
            raise RuntimeError("没有找到 Edge/Chrome，请在“浏览器”页手动选择浏览器 exe。")
        return path

    def start(self) -> None:
        if self.is_cdp_ready():
            self.remember_blank_tab()
            return
        if self.profile_tempdir is None:
            self.profile_tempdir = tempfile.TemporaryDirectory(prefix="ykt-browser-profile-")
            self.profile_dir = Path(self.profile_tempdir.name)
        profile_dir = self.profile_dir
        if profile_dir is None:
            raise RuntimeError("临时浏览器资料目录创建失败。")
        args = [
            self.browser_path(),
            f"--remote-debugging-port={int(self.config.browser_debug_port)}",
            f"--user-data-dir={profile_dir}",
            f"--window-position={int(self.config.browser_window_x)},{int(self.config.browser_window_y)}",
            f"--window-size={max(320, int(self.config.browser_window_w))},{max(240, int(self.config.browser_window_h))}",
            "--new-window",
            "about:blank",
        ]
        self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 10
        while time.time() < deadline:
            if self.is_cdp_ready():
                self.remember_blank_tab()
                return
            time.sleep(0.2)
        raise RuntimeError(f"浏览器已启动，但 DevTools 端口 {self.config.browser_debug_port} 暂不可用。")

    def close(self) -> None:
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
        self.process = None
        if self.profile_tempdir is not None:
            try:
                self.profile_tempdir.cleanup()
            except Exception:
                pass
        self.profile_tempdir = None
        self.profile_dir = None

    def is_cdp_ready(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.cdp_base_url}/json/version", timeout=0.5) as response:
                return response.status == 200
        except Exception:
            return False

    def cdp_json(self, path: str, method: str = "GET") -> dict | list:
        request = urllib.request.Request(f"{self.cdp_base_url}{path}", method=method)
        with urllib.request.urlopen(request, timeout=5) as response:
            raw = response.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw else {}

    def remember_blank_tab(self) -> None:
        if self.current_tab_id:
            return
        try:
            tabs = self.cdp_json("/json/list")
        except Exception:
            return
        if not isinstance(tabs, list):
            return
        for tab in tabs:
            if tab.get("type") == "page" and tab.get("url") == "about:blank":
                self.current_tab_id = str(tab.get("id", ""))
                return

    def open_url(self, url: str) -> dict:
        self.start()
        encoded = urllib.parse.quote(url, safe="")
        try:
            tab = self.cdp_json(f"/json/new?{encoded}", method="PUT")
        except urllib.error.HTTPError:
            tab = self.cdp_json(f"/json/new?{encoded}", method="GET")
        if isinstance(tab, dict):
            tab_id = str(tab.get("id", ""))
            if tab_id:
                try:
                    self.cdp_json(f"/json/activate/{tab_id}")
                except Exception:
                    pass
                old_tab_id = self.current_tab_id
                self.current_tab_id = tab_id
                if old_tab_id and old_tab_id != tab_id:
                    self.close_tab(old_tab_id)
            return tab
        return {}

    def close_tab(self, tab_id: str) -> None:
        try:
            self.cdp_json(f"/json/close/{urllib.parse.quote(tab_id, safe='')}")
        except Exception:
            pass


class PaddleWorker:
    def __init__(self, config: PaddleAppConfig):
        self.config = config
        self._ocr: PaddleOCR | None = None
        self._ocr_cache: dict | None = None
        self._ocr_cache_key: tuple | None = None
        self._similarity_history: list[dict] = []
        self._similarity_sequence = 0
        self._similarity_debug: dict = {}

    @property
    def ocr(self) -> PaddleOCR:
        if self._ocr is None:
            self._ocr = PaddleOCR(
                lang=self.config.paddle_lang or "ch",
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
            )
        return self._ocr

    def screenshot(self) -> Image.Image:
        return ImageGrab.grab(all_screens=True)

    def crop_region(self, image: Image.Image, region: Region | None = None) -> Image.Image:
        region = region or self.config.region
        return image.crop(region.box())

    def cache_key(self, region: Region | None = None) -> tuple:
        region = region or self.config.region
        return (
            region.x,
            region.y,
            region.w,
            region.h,
            self.config.paddle_lang,
            self.config.use_choice_formatter,
        )

    def invalidate_cache(self) -> None:
        self._ocr_cache = None
        self._ocr_cache_key = None

    def reset_similarity_reference(self) -> None:
        self._similarity_history = []
        self._similarity_sequence = 0
        self._similarity_debug = {}

    def image_similarity(self, left: Image.Image, right: Image.Image) -> float:
        return self.image_similarity_details(left, right)["similarity"]

    def image_similarity_details(self, left: Image.Image, right: Image.Image) -> dict:
        legacy_sample_size = (64, 64)
        left_legacy = left.convert("L").resize(legacy_sample_size)
        right_legacy = right.convert("L").resize(legacy_sample_size)
        legacy_diff = ImageChops.difference(left_legacy, right_legacy)
        legacy_mean_diff = ImageStat.Stat(legacy_diff).mean[0]
        legacy_similarity = max(0.0, min(1.0, 1.0 - (legacy_mean_diff / 255.0)))

        sample_size = (256, 256)
        foreground_threshold = 245
        difference_threshold = 18
        left_sample = left.convert("L").resize(sample_size)
        right_sample = right.convert("L").resize(sample_size)
        diff = ImageChops.difference(left_sample, right_sample)
        left_pixels = list(left_sample.getdata())
        right_pixels = list(right_sample.getdata())
        diff_pixels = list(diff.getdata())
        foreground_count = 0
        changed_count = 0
        foreground_diff_total = 0.0
        for left_value, right_value, diff_value in zip(left_pixels, right_pixels, diff_pixels):
            if left_value < foreground_threshold or right_value < foreground_threshold:
                foreground_count += 1
                foreground_diff_total += diff_value
                if diff_value > difference_threshold:
                    changed_count += 1

        if foreground_count:
            changed_ratio = changed_count / foreground_count
            mean_diff = foreground_diff_total / foreground_count
            similarity = max(0.0, min(1.0, 1.0 - changed_ratio))
        else:
            changed_ratio = 0.0
            mean_diff = legacy_mean_diff
            similarity = legacy_similarity

        return {
            "similarity": similarity,
            "mean_diff": mean_diff,
            "sample_size": sample_size,
            "current_size": right.size,
            "previous_size": left.size,
            "legacy_similarity": legacy_similarity,
            "legacy_mean_diff": legacy_mean_diff,
            "legacy_sample_size": legacy_sample_size,
            "foreground_threshold": foreground_threshold,
            "difference_threshold": difference_threshold,
            "foreground_pixels": foreground_count,
            "changed_pixels": changed_count,
            "changed_ratio": changed_ratio,
        }

    def similarity_lag(self, value) -> int:
        try:
            return max(1, min(4, int(float(value))))
        except (TypeError, ValueError):
            return 1

    def compare_with_previous_screenshot(self, image: Image.Image, threshold_percent: float = 90.0, lag: int = 1) -> dict:
        try:
            threshold_value = float(threshold_percent)
        except (TypeError, ValueError):
            threshold_value = 90.0
        threshold = max(0.0, min(100.0, threshold_value))
        lag = self.similarity_lag(lag)
        history_count_before = len(self._similarity_history)
        previous_entry = self._similarity_history[-lag] if history_count_before >= lag else None
        previous = previous_entry["image"] if previous_entry else None
        self._similarity_sequence += 1
        current_entry = {
            "seq": self._similarity_sequence,
            "time": time.strftime("%H:%M:%S"),
            "image": image.copy(),
            "size": image.size,
        }
        self._similarity_history.append(current_entry)
        self._similarity_history = self._similarity_history[-8:]
        history_count_after = len(self._similarity_history)
        if previous is None:
            self._similarity_debug = {
                "current": current_entry,
                "previous": None,
                "history": list(self._similarity_history),
                "comparison": None,
            }
            return {
                "available": False,
                "similarity": None,
                "threshold": threshold,
                "lag": lag,
                "hit": False,
                "reference_updated": True,
                "current_size": image.size,
                "previous_size": None,
                "sample_size": (256, 256),
                "mean_diff": None,
                "legacy_similarity": None,
                "legacy_mean_diff": None,
                "legacy_sample_size": (64, 64),
                "foreground_threshold": 245,
                "difference_threshold": 18,
                "foreground_pixels": None,
                "changed_pixels": None,
                "changed_ratio": None,
                "history_count_before": history_count_before,
                "history_count_after": history_count_after,
                "reason": f"历史截图不足：已有 {history_count_before} 张，需要回看 {lag} 张",
                "current_seq": current_entry["seq"],
                "previous_seq": None,
            }
        details = self.image_similarity_details(previous, image)
        similarity = details["similarity"] * 100
        comparison = {
            "similarity": similarity,
            "threshold": threshold,
            "lag": lag,
            "hit": similarity >= threshold,
            "mean_diff": details["mean_diff"],
            "sample_size": details["sample_size"],
            "history_count_before": history_count_before,
            "history_count_after": history_count_after,
            "current_seq": current_entry["seq"],
            "previous_seq": previous_entry["seq"] if previous_entry else None,
            "legacy_similarity": details["legacy_similarity"] * 100,
            "legacy_mean_diff": details["legacy_mean_diff"],
            "legacy_sample_size": details["legacy_sample_size"],
            "foreground_threshold": details["foreground_threshold"],
            "difference_threshold": details["difference_threshold"],
            "foreground_pixels": details["foreground_pixels"],
            "changed_pixels": details["changed_pixels"],
            "changed_ratio": details["changed_ratio"],
        }
        self._similarity_debug = {
            "current": current_entry,
            "previous": previous_entry,
            "history": list(self._similarity_history),
            "comparison": comparison,
        }
        return {
            "available": True,
            "similarity": similarity,
            "threshold": threshold,
            "lag": lag,
            "hit": similarity >= threshold,
            "reference_updated": True,
            "current_size": details["current_size"],
            "previous_size": details["previous_size"],
            "sample_size": details["sample_size"],
            "mean_diff": details["mean_diff"],
            "legacy_similarity": details["legacy_similarity"] * 100,
            "legacy_mean_diff": details["legacy_mean_diff"],
            "legacy_sample_size": details["legacy_sample_size"],
            "foreground_threshold": details["foreground_threshold"],
            "difference_threshold": details["difference_threshold"],
            "foreground_pixels": details["foreground_pixels"],
            "changed_pixels": details["changed_pixels"],
            "changed_ratio": details["changed_ratio"],
            "history_count_before": history_count_before,
            "history_count_after": history_count_after,
            "reason": "",
            "current_seq": current_entry["seq"],
            "previous_seq": previous_entry["seq"] if previous_entry else None,
        }

    def similarity_debug_snapshot(self) -> dict:
        return {
            "current": self._similarity_debug.get("current"),
            "previous": self._similarity_debug.get("previous"),
            "history": list(self._similarity_debug.get("history") or []),
            "comparison": self._similarity_debug.get("comparison"),
        }

    def screen_point_to_image_point(self, x: int, y: int, image: Image.Image | None = None) -> tuple[int, int]:
        image = image or self.screenshot()
        origin_x, origin_y, screen_w, screen_h = virtual_screen_bounds()
        scale_x = image.size[0] / max(1, screen_w)
        scale_y = image.size[1] / max(1, screen_h)
        image_x = int(round((x - origin_x) * scale_x))
        image_y = int(round((y - origin_y) * scale_y))
        return max(0, min(image_x, image.size[0] - 1)), max(0, min(image_y, image.size[1] - 1))

    def predict_image(self, image: Image.Image) -> dict:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as file:
            temp_path = Path(file.name)
        try:
            image.save(temp_path)
            result = self.ocr.predict(str(temp_path))
            return result[0] if result else {}
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass

    def raw_items(self, image: Image.Image, region: Region | None = None) -> list[dict]:
        region = region or self.config.region
        result = self.predict_image(image)
        texts = result.get("rec_texts")
        scores = result.get("rec_scores")
        boxes = result.get("rec_boxes")
        texts = [] if texts is None else texts
        scores = [] if scores is None else scores
        boxes = [] if boxes is None else boxes
        origin_x, origin_y = virtual_screen_origin()
        items: list[dict] = []
        for index, text in enumerate(texts):
            text = str(text).strip()
            if not text:
                continue
            if index < len(boxes):
                box = boxes[index].tolist() if hasattr(boxes[index], "tolist") else list(boxes[index])
                left, top, right, bottom = [int(round(value)) for value in box]
            else:
                left = top = right = bottom = 0
            width = max(1, right - left)
            height = max(1, bottom - top)
            score = scores[index] if index < len(scores) else None
            try:
                confidence = round(float(score), 4)
            except (TypeError, ValueError):
                confidence = None
            screen_left = origin_x + region.x + left
            screen_top = origin_y + region.y + top
            items.append(
                {
                    "index": len(items) + 1,
                    "text": text,
                    "confidence": confidence,
                    "psm": "paddle",
                    "left": left,
                    "top": top,
                    "width": width,
                    "height": height,
                    "screen_left": screen_left,
                    "screen_top": screen_top,
                    "screen_center_x": screen_left + width // 2,
                    "screen_center_y": screen_top + height // 2,
                }
            )
        return items

    def make_item(
        self,
        text: str,
        left: int,
        top: int,
        width: int,
        height: int,
        region: Region,
        psm: str,
        confidence=None,
    ) -> dict:
        origin_x, origin_y = virtual_screen_origin()
        screen_left = origin_x + region.x + left
        screen_top = origin_y + region.y + top
        return {
            "index": 0,
            "text": text,
            "confidence": confidence,
            "psm": psm,
            "left": int(left),
            "top": int(top),
            "width": max(1, int(width)),
            "height": max(1, int(height)),
            "screen_left": screen_left,
            "screen_top": screen_top,
            "screen_center_x": screen_left + max(1, int(width)) // 2,
            "screen_center_y": screen_top + max(1, int(height)) // 2,
        }

    def is_choice_marker_pixel(self, pixel: tuple[int, int, int]) -> bool:
        r, g, b = pixel
        blue = b > 145 and r < 135 and g < 190
        outline_or_dark = max(r, g, b) < 235 and min(r, g, b) < 225
        status_badge = (g > 140 and r < 80 and b < 160) or (r > 180 and g < 110 and b < 110)
        return blue or outline_or_dark or status_badge

    def detect_choice_markers(self, image: Image.Image) -> list[dict]:
        rgb = image.convert("RGB")
        width, height = rgb.size
        scan_width = min(width, min(180, max(120, int(width * 0.2))))
        visited: set[tuple[int, int]] = set()
        candidates: list[dict] = []

        for start_y in range(height):
            for start_x in range(scan_width):
                if (start_x, start_y) in visited:
                    continue
                if not self.is_choice_marker_pixel(rgb.getpixel((start_x, start_y))):
                    visited.add((start_x, start_y))
                    continue
                stack = [(start_x, start_y)]
                visited.add((start_x, start_y))
                xs = []
                ys = []
                while stack:
                    x, y = stack.pop()
                    xs.append(x)
                    ys.append(y)
                    for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                        if nx < 0 or nx >= scan_width or ny < 0 or ny >= height or (nx, ny) in visited:
                            continue
                        visited.add((nx, ny))
                        if self.is_choice_marker_pixel(rgb.getpixel((nx, ny))):
                            stack.append((nx, ny))

                if not xs:
                    continue
                left, right = min(xs), max(xs)
                top, bottom = min(ys), max(ys)
                box_w = right - left + 1
                box_h = bottom - top + 1
                area = len(xs)
                aspect = box_w / max(1, box_h)
                if 28 <= box_w <= 82 and 28 <= box_h <= 82 and 0.55 <= aspect <= 1.7 and area >= 55:
                    candidates.append(
                        {
                            "left": left,
                            "top": top,
                            "width": box_w,
                            "height": box_h,
                            "center_x": left + box_w // 2,
                            "center_y": top + box_h // 2,
                            "area": area,
                        }
                    )

        candidates.sort(key=lambda item: (item["center_y"], item["center_x"]))
        deduped: list[dict] = []
        for candidate in candidates:
            duplicate = next(
                (
                    item
                    for item in deduped
                    if abs(candidate["center_y"] - item["center_y"]) < 18 and abs(candidate["center_x"] - item["center_x"]) < 28
                ),
                None,
            )
            if duplicate:
                if candidate["area"] > duplicate["area"]:
                    deduped.remove(duplicate)
                    deduped.append(candidate)
                continue
            deduped.append(candidate)

        if len(deduped) > 6:
            clusters = []
            for candidate in deduped:
                cluster = [item for item in deduped if abs(item["center_x"] - candidate["center_x"]) <= 34]
                clusters.append(cluster)
            cluster = max(clusters, key=lambda group: (len(group), sum(item["area"] for item in group)))
            cluster.sort(key=lambda item: item["center_y"])
            deduped = cluster
        return sorted(deduped, key=lambda item: item["center_y"])[:6]

    def is_judgement_marker_layout(self, markers: list[dict]) -> bool:
        if len(markers) != 2:
            return False
        left, right = sorted(markers, key=lambda item: item["center_x"])
        same_row = abs(left["center_y"] - right["center_y"]) <= max(18, int(max(left["height"], right["height"]) * 0.45))
        horizontal_gap = right["center_x"] - left["center_x"]
        similar_size = abs(left["width"] - right["width"]) <= 16 and abs(left["height"] - right["height"]) <= 16
        return same_row and 35 <= horizontal_gap <= 140 and similar_size

    def append_visual_judgement_markers(self, image: Image.Image, items: list[dict], region: Region) -> list[dict]:
        markers = self.detect_choice_markers(image)
        if not self.is_judgement_marker_layout(markers):
            return items
        for label, marker in zip(("正确", "错误"), sorted(markers, key=lambda item: item["center_x"])):
            already_exists = False
            for item in items:
                normalized = self.normalize_answer_token(item.get("text", ""))
                item_center_x = item["left"] + item["width"] / 2
                item_center_y = item["top"] + item["height"] / 2
                if normalized == label and abs(item_center_x - marker["center_x"]) <= 24 and abs(item_center_y - marker["center_y"]) <= 24:
                    already_exists = True
                    break
            if not already_exists:
                items.append(
                    self.make_item(
                        label,
                        marker["left"],
                        marker["top"],
                        marker["width"],
                        marker["height"],
                        region,
                        "visual_judgement",
                        None,
                    )
                )
        return items

    def append_visual_choice_markers(self, image: Image.Image, items: list[dict], region: Region) -> list[dict]:
        markers = self.detect_choice_markers(image)
        if self.is_judgement_marker_layout(markers):
            return items
        if not markers:
            return items
        letters = "ABCDEF"

        existing_letter_items = []
        for item in items:
            normalized = self.normalize_choice_letter(item.get("text", ""))
            if normalized not in letters or len(normalized) != 1:
                continue
            if item.get("psm") in {"visual_nav", "visual_judgement"}:
                continue
            if item.get("left", 0) <= 170 and item.get("width", 999) <= 95 and item.get("height", 999) <= 95:
                existing_letter_items.append((normalized, item))

        if not existing_letter_items and len(markers) < 2:
            return items

        assigned: list[tuple[str, dict]] = []
        used_existing_ids = set()
        unmatched_markers: list[tuple[int, dict]] = []
        for marker_index, marker in enumerate(markers[: len(letters)]):
            marker_center_y = marker["center_y"]
            marker_center_x = marker["center_x"]
            best = None
            best_distance = None
            for letter, item in existing_letter_items:
                item_id = id(item)
                if item_id in used_existing_ids:
                    continue
                item_center_y = item["top"] + item["height"] / 2
                item_center_x = item["left"] + item["width"] / 2
                distance_y = abs(item_center_y - marker_center_y)
                distance_x = abs(item_center_x - marker_center_x)
                tolerance_y = max(28, marker["height"] * 0.9, item["height"] * 1.15)
                tolerance_x = max(70, marker["width"] * 1.6)
                if distance_y <= tolerance_y and distance_x <= tolerance_x and (best_distance is None or distance_y < best_distance):
                    best = (letter, item)
                    best_distance = distance_y
            if best:
                letter, item = best
                assigned.append((letter, marker))
                used_existing_ids.add(id(item))
            else:
                unmatched_markers.append((marker_index, marker))

        assigned_letters = {letter for letter, _marker in assigned}
        if not existing_letter_items:
            assigned.extend((letters[index], marker) for index, marker in unmatched_markers if index < len(letters))
        else:
            existing_by_letter = {
                letter: item["top"] + item["height"] / 2
                for letter, item in existing_letter_items
                if letter in letters
            }
            spacings = []
            sorted_existing = sorted(existing_by_letter.items(), key=lambda pair: letters.index(pair[0]))
            for (left_letter, left_y), (right_letter, right_y) in zip(sorted_existing, sorted_existing[1:]):
                step = letters.index(right_letter) - letters.index(left_letter)
                if step > 0:
                    spacings.append((right_y - left_y) / step)
            default_spacing = max(42.0, sum(spacings) / len(spacings)) if spacings else 62.0

            def expected_y_for(letter: str) -> float | None:
                letter_index = letters.index(letter)
                previous = [(letters.index(existing_letter), y) for existing_letter, y in existing_by_letter.items() if letters.index(existing_letter) < letter_index]
                following = [(letters.index(existing_letter), y) for existing_letter, y in existing_by_letter.items() if letters.index(existing_letter) > letter_index]
                if previous and following:
                    prev_index, prev_y = max(previous, key=lambda pair: pair[0])
                    next_index, next_y = min(following, key=lambda pair: pair[0])
                    ratio = (letter_index - prev_index) / max(1, next_index - prev_index)
                    return prev_y + (next_y - prev_y) * ratio
                if previous:
                    prev_index, prev_y = max(previous, key=lambda pair: pair[0])
                    return prev_y + default_spacing * (letter_index - prev_index)
                if following:
                    next_index, next_y = min(following, key=lambda pair: pair[0])
                    return next_y - default_spacing * (next_index - letter_index)
                return None

            for _marker_index, marker in unmatched_markers:
                marker_center_y = marker["center_y"]
                candidates = []
                for letter in letters:
                    if letter in assigned_letters or letter in existing_by_letter:
                        continue
                    expected_y = expected_y_for(letter)
                    if expected_y is None:
                        continue
                    candidates.append((abs(marker_center_y - expected_y), letter))
                if not candidates:
                    continue
                distance, inferred_letter = min(candidates, key=lambda value: value[0])
                if distance > max(36, marker["height"] * 1.1, default_spacing * 0.65):
                    continue
                assigned.append((inferred_letter, marker))
                assigned_letters.add(inferred_letter)

        for letter, marker in assigned:
            center_y = marker["center_y"]
            already_exists = False
            for item in items:
                item_center_y = item["top"] + item["height"] / 2
                item_center_x = item["left"] + item["width"] / 2
                if (
                    self.normalize_choice_letter(item["text"]) == letter
                    and abs(item_center_y - center_y) <= 22
                    and item_center_x <= marker["center_x"] + marker["width"] + 24
                ):
                    already_exists = True
                    break
            if not already_exists:
                items.append(
                    self.make_item(
                        letter,
                        marker["left"],
                        marker["top"],
                        marker["width"],
                        marker["height"],
                        region,
                        "visual_choice",
                        None,
                    )
                )
        return items

    def count_dark_pixels(self, image: Image.Image, box: tuple[int, int, int, int]) -> int:
        rgb = image.convert("RGB")
        left, top, right, bottom = box
        count = 0
        for y in range(max(0, top), min(rgb.height, bottom)):
            for x in range(max(0, left), min(rgb.width, right)):
                r, g, b = rgb.getpixel((x, y))
                if max(r, g, b) < 140:
                    count += 1
        return count

    def dark_pixel_bounds(self, image: Image.Image, box: tuple[int, int, int, int]) -> tuple[int, int, int, int] | None:
        rgb = image.convert("RGB")
        left, top, right, bottom = box
        xs = []
        ys = []
        for y in range(max(0, top), min(rgb.height, bottom)):
            for x in range(max(0, left), min(rgb.width, right)):
                r, g, b = rgb.getpixel((x, y))
                dark_text = max(r, g, b) < 165 and min(r, g, b) < 145
                blue_text = b > 145 and r < 155 and g < 195
                if dark_text or blue_text:
                    xs.append(x)
                    ys.append(y)
        if len(xs) < 12:
            return None
        pad_x = 14
        pad_y = 10
        return (
            max(0, min(xs) - pad_x),
            max(0, min(ys) - pad_y),
            min(rgb.width, max(xs) + pad_x),
            min(rgb.height, max(ys) + pad_y),
        )

    def count_blue_pixels(self, image: Image.Image, box: tuple[int, int, int, int]) -> int:
        rgb = image.convert("RGB")
        left, top, right, bottom = box
        count = 0
        for y in range(max(0, top), min(rgb.height, bottom)):
            for x in range(max(0, left), min(rgb.width, right)):
                r, g, b = rgb.getpixel((x, y))
                if b > 145 and r < 145 and g < 190:
                    count += 1
        return count

    def count_light_button_pixels(self, image: Image.Image, box: tuple[int, int, int, int]) -> int:
        rgb = image.convert("RGB")
        left, top, right, bottom = box
        count = 0
        for y in range(max(0, top), min(rgb.height, bottom)):
            for x in range(max(0, left), min(rgb.width, right)):
                r, g, b = rgb.getpixel((x, y))
                if 225 <= r <= 250 and 225 <= g <= 250 and 225 <= b <= 250 and max(r, g, b) - min(r, g, b) <= 8:
                    count += 1
        return count

    def has_text_like_item(self, items: list[dict], text: str) -> bool:
        return any(self.text_matches(text, item.get("text", "")) for item in items)

    def append_navigation_hints(self, image: Image.Image, items: list[dict], region: Region) -> list[dict]:
        width, height = image.size
        if width < 360 or height < 260:
            return items
        band_top = max(0, height - max(90, int(height * 0.12)))
        left_box = (0, band_top, int(width * 0.22), height)
        center_box = (int(width * 0.36), band_top, int(width * 0.64), height)
        right_box = (int(width * 0.78), band_top, width, height)

        for label, box in (("上一题", left_box), ("下一题", right_box)):
            bounds = self.dark_pixel_bounds(image, box)
            if bounds:
                left, top, right, bottom = bounds
                items.append(self.make_item(label, left, top, right - left, bottom - top, region, "visual_nav"))

        blue_pixels = self.count_blue_pixels(image, center_box)
        light_pixels = self.count_light_button_pixels(image, center_box)
        center_area = max(1, (center_box[2] - center_box[0]) * (center_box[3] - center_box[1]))
        if blue_pixels >= 120:
            bounds = self.dark_pixel_bounds(image, center_box) or center_box
            left, top, right, bottom = bounds
            items.append(self.make_item("提交", left, top, right - left, bottom - top, region, "visual_nav"))
        elif light_pixels / center_area >= 0.28:
            items.append(self.make_item("已提交", center_box[0], band_top, center_box[2] - center_box[0], height - band_top, region, "visual_nav"))
        return items

    def normalize_choice_letter(self, text: str) -> str:
        normalized = text.strip().upper().replace(".", "").replace("。", "")
        if normalized == "C":
            return "C"
        return normalized

    def normalize_answer_token(self, text: str) -> str:
        raw = str(text or "").strip()
        compact = re.sub(r"[\s。．.、,，:：;；（）()【】\[\]<>《》]+", "", raw)
        upper = compact.upper()
        if upper in {"A", "B", "C", "D", "E", "F"}:
            return upper
        if compact in {"正确", "对", "是", "真", "√", "✓", "✔", "勾"} or upper in {"TRUE", "T", "YES", "Y"}:
            return "正确"
        if compact in {"错误", "错", "否", "假", "×", "✕", "✖", "叉"} or upper in {"FALSE", "NO", "N"}:
            return "错误"
        return compact or upper

    def normalize_text_for_match(self, text: str) -> str:
        text = str(text or "").upper()
        return re.sub(r"[\s,，、。．.；;:：!?！？（）()【】\[\]<>《》\"'“”‘’\-—_]+", "", text)

    def text_matches(self, needle: str, haystack: str) -> bool:
        needle = str(needle or "").strip()
        haystack = str(haystack or "").strip()
        if not needle or not haystack:
            return False
        if needle in haystack:
            return True
        needle_norm = self.normalize_text_for_match(needle)
        haystack_norm = self.normalize_text_for_match(haystack)
        if len(needle_norm) < 2 or len(haystack_norm) < 2:
            return False
        if needle_norm in haystack_norm:
            return True
        return len(haystack_norm) >= 3 and haystack_norm in needle_norm

    def compact_items_text(self, items: list[dict]) -> str:
        if not items:
            return ""
        lines: list[list[dict]] = []
        for item in sorted(items, key=lambda value: (value["top"], value["left"])):
            center_y = item["top"] + item["height"] / 2
            for line in lines:
                line_center = sum(entry["top"] + entry["height"] / 2 for entry in line) / len(line)
                if abs(center_y - line_center) <= max(14, item["height"] * 0.65):
                    line.append(item)
                    break
            else:
                lines.append([item])
        text_lines = []
        for line in lines:
            parts = [entry["text"].strip() for entry in sorted(line, key=lambda value: value["left"]) if entry["text"].strip()]
            if parts:
                text_lines.append(" ".join(parts))
        return "\n".join(text_lines).strip()

    def raw_items_text(self, items: list[dict]) -> str:
        return self.compact_items_text([item for item in items if item.get("text", "").strip()])

    def combined_search_text(self, *texts: str) -> str:
        lines = []
        seen = set()
        for text in texts:
            for line in str(text or "").splitlines():
                line = line.strip()
                if line and line not in seen:
                    lines.append(line)
                    seen.add(line)
        return "\n".join(lines)

    def choice_marker_items(self, items: list[dict]) -> list[tuple[str, dict]]:
        option_letters = {"A", "B", "C", "D", "E", "F"}
        lines = sorted(items, key=lambda item: (item["top"], item["left"]))
        visual_markers = []
        fallback_markers = []
        for item in lines:
            normalized = self.normalize_choice_letter(item["text"])
            if normalized not in option_letters or len(normalized) != 1:
                continue
            if item.get("psm") == "visual_choice":
                visual_markers.append((normalized, item))
            elif item.get("left", 0) <= 150 and item.get("width", 999) <= 95 and item.get("height", 999) <= 95:
                fallback_markers.append((normalized, item))

        if visual_markers:
            fallback_by_letter = {}
            for letter, item in fallback_markers:
                fallback_by_letter.setdefault(letter, item)
            by_letter = {letter: (letter, item) for letter, item in fallback_by_letter.items()}
            for letter, item in visual_markers:
                fallback = fallback_by_letter.get(letter)
                item_center_y = item["top"] + item["height"] / 2
                if fallback:
                    fallback_center_y = fallback["top"] + fallback["height"] / 2
                    if abs(item_center_y - fallback_center_y) <= max(30, item["height"], fallback["height"]):
                        by_letter[letter] = (letter, item)
                    continue
                nearby_real_letter = any(
                    abs(item_center_y - (fallback_item["top"] + fallback_item["height"] / 2)) <= max(26, item["height"])
                    for fallback_item in fallback_by_letter.values()
                )
                if not nearby_real_letter:
                    by_letter[letter] = (letter, item)
            markers = sorted(by_letter.values(), key=lambda pair: (pair[1]["top"], pair[1]["left"]))
        else:
            markers = fallback_markers
        deduped: list[tuple[str, dict]] = []
        used_letters = set()
        for letter, item in markers:
            if letter in used_letters:
                continue
            deduped.append((letter, item))
            used_letters.add(letter)
        return deduped

    def choice_structure(self, items: list[dict]) -> tuple[str, dict[str, dict], list[dict]]:
        lines = sorted(items, key=lambda item: (item["top"], item["left"]))
        option_letters = {"A", "B", "C", "D", "E", "F"}
        letter_items = self.choice_marker_items(lines)
        marker_ids = {id(marker) for _letter, marker in letter_items}

        options: dict[str, dict] = {}
        used_ids = set()
        for index, (letter, marker) in enumerate(letter_items):
            marker_center_y = marker["top"] + marker["height"] / 2
            previous_center_y = None
            next_center_y = None
            if index > 0:
                previous_marker = letter_items[index - 1][1]
                previous_center_y = previous_marker["top"] + previous_marker["height"] / 2
            if index + 1 < len(letter_items):
                next_marker = letter_items[index + 1][1]
                next_center_y = next_marker["top"] + next_marker["height"] / 2

            if previous_center_y is not None:
                band_top = (previous_center_y + marker_center_y) / 2
            else:
                next_spacing = (next_center_y - marker_center_y) if next_center_y is not None else max(62, marker["height"] * 2)
                band_top = marker_center_y - max(30, next_spacing * 0.48)

            if next_center_y is not None:
                band_bottom = (marker_center_y + next_center_y) / 2
            else:
                previous_spacing = (marker_center_y - previous_center_y) if previous_center_y is not None else max(62, marker["height"] * 2)
                band_bottom = marker_center_y + max(70, previous_spacing * 1.1)

            text_candidates = []
            for item in lines:
                if id(item) in marker_ids:
                    continue
                normalized = self.normalize_choice_letter(item["text"])
                if normalized in option_letters and len(normalized) == 1:
                    continue
                if item.get("psm") == "visual_nav":
                    continue
                if item["left"] <= marker["left"] + marker["width"] * 0.7:
                    continue
                item_center_y = item["top"] + item["height"] / 2
                if band_top <= item_center_y <= band_bottom:
                    text_candidates.append(item)

            text_candidates.sort(key=lambda item: (item["top"], item["left"]))
            value_item = text_candidates[0] if text_candidates else marker
            options[letter] = {
                "letter": letter,
                "text": self.compact_items_text(text_candidates),
                "letter_item": marker,
                "text_item": value_item,
            }
            used_ids.add(id(marker))
            for item in text_candidates:
                used_ids.add(id(item))

        first_option_top = min((marker["top"] for _letter, marker in letter_items), default=None)
        last_option_bottom = max(
            (
                max(
                    option["letter_item"]["top"] + option["letter_item"]["height"],
                    option["text_item"]["top"] + option["text_item"]["height"],
                )
                for option in options.values()
            ),
            default=None,
        )
        question_items = []
        extra_items = []
        for item in lines:
            if id(item) in used_ids:
                continue
            if item.get("psm") == "visual_judgement":
                continue
            normalized = self.normalize_choice_letter(item["text"])
            if normalized in option_letters and len(normalized) == 1:
                continue
            if first_option_top is not None and item["top"] >= first_option_top - 8:
                if last_option_bottom is not None and item["top"] <= last_option_bottom + 12:
                    extra_items.append(item)
                    continue
                extra_items.append(item)
                continue
            question_items.append(item)
        question = self.compact_items_text(question_items)
        return question, options, extra_items

    def judgement_structure(self, items: list[dict]) -> dict[str, dict]:
        markers: dict[str, dict] = {}
        for item in sorted(items, key=lambda value: (value["top"], value["left"])):
            normalized = self.normalize_answer_token(item.get("text", ""))
            if normalized not in {"正确", "错误"}:
                continue
            if item.get("psm") == "visual_judgement" or (item.get("width", 999) <= 95 and item.get("height", 999) <= 95):
                markers.setdefault(normalized, item)
        return markers

    def format_text(self, items: list[dict]) -> str:
        if not self.config.use_choice_formatter:
            return "\n".join(item["text"] for item in items).strip()

        question, options, extra_items = self.choice_structure(items)
        judgement_options = self.judgement_structure(items)
        option_text = "\n".join(f"{letter}. {options[letter]['text']}" for letter in sorted(options))
        extra_text = self.compact_items_text(extra_items)
        parts = []
        if question:
            parts.append(question)
        if option_text:
            parts.append(f"选项：\n{option_text}")
        elif {"正确", "错误"}.issubset(judgement_options):
            parts.append("判断选项：\n正确\n错误")
        if extra_text:
            parts.append(f"其他文字：\n{extra_text}")
        return "\n\n".join(parts) or "\n".join(item["text"] for item in sorted(items, key=lambda item: (item["top"], item["left"]))).strip()

    def choice_position(
        self,
        target: str,
        region: Region | None = None,
        click_area: str = "letter",
    ) -> tuple[int, int] | None:
        result = self.locate_target(target, region, click_area)
        return result["pos"] if result else None

    def locate_target(
        self,
        target: str,
        region: Region | None = None,
        click_area: str = "letter",
    ) -> dict | None:
        region = region or self.config.region
        details = self.ocr_region_details(force=False, region=region)
        items = details["items"]
        needle = target.strip()
        normalized = self.normalize_choice_letter(needle)
        normalized_answer = self.normalize_answer_token(needle)
        nav_targets = {"上一题", "下一题", "提交", "已提交"}

        if needle in nav_targets:
            nav_items = [
                item
                for item in items
                if item.get("psm") == "visual_nav" and item.get("text", "") == needle
            ]
            if nav_items:
                item = sorted(nav_items, key=lambda value: value.get("width", 0) * value.get("height", 0))[0]
                return {
                    "pos": (item["screen_center_x"], item["screen_center_y"]),
                    "item": item,
                    "details": details,
                    "label": item.get("text", needle),
                    "cache_hit": bool(details.get("cache_hit")),
                }
            exact_items = [
                item
                for item in items
                if item.get("psm") != "visual_nav" and item.get("text", "").strip() == needle
            ]
            if exact_items:
                item = sorted(exact_items, key=lambda value: (value.get("top", 0), value.get("left", 0)))[0]
                return {
                    "pos": (item["screen_center_x"], item["screen_center_y"]),
                    "item": item,
                    "details": details,
                    "label": item.get("text", needle),
                    "cache_hit": bool(details.get("cache_hit")),
                }
            return None

        if self.config.use_choice_formatter:
            judgement_options = self.judgement_structure(items)
            if normalized_answer in judgement_options:
                item = judgement_options[normalized_answer]
                return {
                    "pos": (item["screen_center_x"], item["screen_center_y"]),
                    "item": item,
                    "details": details,
                    "label": normalized_answer,
                    "cache_hit": bool(details.get("cache_hit")),
                }
            _question, options, _extras = self.choice_structure(items)
            for letter, option in options.items():
                text = option["text"]
                if normalized == letter or self.text_matches(needle, text):
                    item = option["text_item"] if click_area == "text" else option["letter_item"]
                    return {
                        "pos": (item["screen_center_x"], item["screen_center_y"]),
                        "item": item,
                        "details": details,
                        "label": f"{letter}. {text}",
                        "cache_hit": bool(details.get("cache_hit")),
                    }

        for item in items:
            if self.text_matches(needle, item["text"]):
                return {
                    "pos": (item["screen_center_x"], item["screen_center_y"]),
                    "item": item,
                    "details": details,
                    "label": item["text"],
                    "cache_hit": bool(details.get("cache_hit")),
                }
        return None

    def ocr_region_details(self, force: bool = False, region: Region | None = None) -> dict:
        region = region or self.config.region
        key = self.cache_key(region)
        if not force and self._ocr_cache is not None and self._ocr_cache_key == key:
            self._ocr_cache["cache_hit"] = True
            return self._ocr_cache

        screenshot = self.screenshot()
        crop = self.crop_region(screenshot, region)
        items = self.raw_items(crop, region)
        items = self.append_visual_judgement_markers(crop, items, region)
        items = self.append_visual_choice_markers(crop, items, region)
        items = self.append_navigation_hints(crop, items, region)
        for index, item in enumerate(sorted(items, key=lambda value: (value["top"], value["left"])), start=1):
            item["index"] = index
        q_text = self.format_text(items)
        raw_text = self.raw_items_text(items)
        details = {
            "region": asdict(region),
            "image": crop,
            "original_image": crop,
            "items": items,
            "choices": [],
            "text": q_text,
            "raw_text": raw_text,
            "search_text": self.combined_search_text(q_text, raw_text),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "preprocess": "PaddleOCR PP-OCRv5",
            "cache_hit": False,
        }
        self._ocr_cache = details
        self._ocr_cache_key = key
        return details

    def annotate_ocr_image(self, image: Image.Image, items: list[dict], highlight_item: dict | None = None) -> Image.Image:
        annotated = image.convert("RGB")
        draw = ImageDraw.Draw(annotated)
        for item in items:
            left = item["left"]
            top = item["top"]
            right = left + item["width"]
            bottom = top + item["height"]
            is_highlight = self.same_item(item, highlight_item) if highlight_item else False
            color = "#13a10e" if is_highlight else "#2277ff"
            width = 5 if is_highlight else 3
            draw.rectangle((left, top, right, bottom), outline=color, width=width)
            label = str(item["index"])
            label_box = draw.textbbox((0, 0), label)
            label_w = label_box[2] - label_box[0] + 8
            label_h = label_box[3] - label_box[1] + 6
            y = max(0, top - label_h)
            draw.rectangle((left, y, left + label_w, y + label_h), fill=color)
            draw.text((left + 4, y + 3), label, fill="#ffffff")
            if is_highlight:
                center_x = left + item["width"] // 2
                center_y = top + item["height"] // 2
                draw.line((center_x - 14, center_y, center_x + 14, center_y), fill=color, width=3)
                draw.line((center_x, center_y - 14, center_x, center_y + 14), fill=color, width=3)
        return annotated

    def same_item(self, item: dict | None, other: dict | None) -> bool:
        if not item or not other:
            return False
        return (
            item.get("index") == other.get("index")
            and item.get("left") == other.get("left")
            and item.get("top") == other.get("top")
            and item.get("text") == other.get("text")
        )

    def find_text_position(self, text: str, region: Region | None = None) -> tuple[int, int] | None:
        result = self.locate_target(text, region, click_area="letter")
        return result["pos"] if result else None

    def find_choice_position(self, letter: str, click_area: str = "letter") -> tuple[int, int] | None:
        return self.choice_position(letter, self.config.region, click_area=click_area)

    def locate_choice(self, letter: str, click_area: str = "letter") -> dict | None:
        return self.locate_target(letter, self.config.region, click_area=click_area)

    def click_choice(self, letter: str, click_area: str = "letter") -> tuple[int, int] | None:
        pos = self.find_choice_position(letter, click_area=click_area)
        if pos:
            self.click_position(*pos)
        return pos

    def click_position(self, x: int, y: int) -> None:
        import ctypes
        from ctypes import wintypes

        x = int(x)
        y = int(y)
        ctypes.windll.user32.SetCursorPos(x, y)
        try:
            point = wintypes.POINT(x, y)
            hwnd = ctypes.windll.user32.WindowFromPoint(point)
            if hwnd:
                root_hwnd = ctypes.windll.user32.GetAncestor(hwnd, 2)
                if root_hwnd:
                    hwnd = root_hwnd
                ctypes.windll.user32.SetForegroundWindow(hwnd)
                time.sleep(0.08)
                ctypes.windll.user32.SetCursorPos(x, y)
        except Exception:
            pass
        ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
        ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)

    def click_text(self, text: str, region: Region | None = None) -> tuple[int, int] | None:
        pos = self.find_text_position(text, region)
        if pos:
            self.click_position(*pos)
        return pos

    def split_condition_terms(self, text: str) -> list[str]:
        text = text.strip()
        if not text:
            return []
        if "&&" in text:
            parts = text.split("&&")
        elif "\n" in text:
            parts = text.splitlines()
        else:
            parts = re.split(r"[,，、;；]", text)
        return [part.strip() for part in parts if part.strip()]

    def render_template(self, template: str, context: dict) -> str:
        values = {
            "Q": context.get("q", ""),
            "U": self.config.prompt_u.strip(),
            "AI": context.get("ai_answer", ""),
            "AI_ANSWER": context.get("ai_answer", ""),
            "OCR": context.get("ocr_raw_text", "") or context.get("ocr_search_text", "") or context.get("q", ""),
            "MATCHES": ",".join(context.get("matches") or []),
            "PARSED": context.get("parsed_text", ""),
        }
        result = template or ""
        for key, value in values.items():
            result = result.replace("{" + key + "}", str(value))
            result = result.replace("{" + key.lower() + "}", str(value))
        return result

    def source_text(self, source: str, context: dict) -> str:
        if source == "ai":
            return context.get("ai_answer", "")
        if not context.get("ocr_details"):
            details = self.ocr_region_details(force=False)
            context["ocr_details"] = details
            context["q"] = details.get("text", "")
            context["ocr_raw_text"] = details.get("raw_text", "")
            context["ocr_search_text"] = details.get("search_text", "")
        details = context.get("ocr_details") or {}
        return details.get("search_text") or details.get("raw_text") or context.get("q", "")

    def answer_text_candidates(self, details: dict | None) -> list[str]:
        if not details:
            return []
        items = details.get("items") or []
        candidates: list[str] = []
        try:
            _question, options, _extra_items = self.choice_structure(items)
            candidates.extend(option.get("text", "") for option in options.values())
        except Exception:
            pass
        ignored = {
            "上一题",
            "下一题",
            "提交",
            "已提交",
            "正确答案",
            "我的答案",
            "本题得分",
            "单选题",
            "多选题",
            "判断题",
            "选项",
        }
        for item in sorted(items, key=lambda value: (value.get("top", 0), value.get("left", 0))):
            text = str(item.get("text", "")).strip()
            normalized = self.normalize_answer_token(text)
            if not text or normalized in {"A", "B", "C", "D", "E", "F", "正确", "错误"}:
                continue
            if any(word in text for word in ignored):
                continue
            if len(self.normalize_text_for_match(text)) >= 3:
                candidates.append(text)
        deduped = []
        seen = set()
        for candidate in candidates:
            candidate = re.sub(r"\s+", " ", str(candidate or "")).strip(" \t\r\n：:。.；;")
            key = self.normalize_text_for_match(candidate)
            if not key or key in seen:
                continue
            deduped.append(candidate)
            seen.add(key)
        return deduped

    def clean_answer_segment(self, text: str) -> str:
        text = str(text or "").strip()
        text = re.sub(r"^(?:正确答案|答案|选择|选项|应选|判断)\s*[:：]\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*(?:回答|解析|理由)\s*[:：].*$", "", text, flags=re.IGNORECASE | re.DOTALL)
        return text.strip(" \t\r\n。；;")

    def split_answer_texts(self, text: str) -> list[str]:
        text = self.clean_answer_segment(text)
        if not text:
            return []
        if "\n" in text:
            parts = text.splitlines()
        else:
            parts = re.split(r"[,，;；|/]+", text)
        results = []
        for part in parts:
            part = re.sub(r"^\s*(?:[-*•]|\d+[.)、]|[A-Fa-f][.、])\s*", "", part)
            part = part.strip(" \t\r\n。；;，,、")
            if part:
                results.append(part)
        return results

    def answer_letter_tokens(self, text: str) -> list[str]:
        letters = []
        text = self.clean_answer_segment(text)
        if not text:
            return letters
        parts = text.splitlines() if "\n" in text else re.split(r"[,，;；|/]+", text)
        for part in parts:
            compact = re.sub(r"[\s.．。:：、]+", "", part.upper())
            if re.fullmatch(r"[A-F]+", compact):
                for letter in compact:
                    if letter not in letters:
                        letters.append(letter)
                continue
            match = re.match(r"^\s*([A-Fa-f])(?:[.．。、:：)\]】\s]+)", part)
            if match:
                letter = match.group(1).upper()
                if letter not in letters:
                    letters.append(letter)
        return letters

    def should_use_full_answer_segment(self, selected: str, full_segment: str) -> bool:
        selected = self.clean_answer_segment(selected)
        full_segment = self.clean_answer_segment(full_segment)
        if not selected or not full_segment or selected == full_segment:
            return False
        selected_norm = self.normalize_text_for_match(selected)
        full_norm = self.normalize_text_for_match(full_segment)
        if len(full_norm) <= len(selected_norm):
            return False
        selected_tail = selected.rstrip()
        looks_truncated = selected_tail.endswith((",", "，", "、", ";", "；", "|", "/"))
        selected_letters_only = bool(re.fullmatch(r"[A-Fa-f\s,，、;；|/]+", selected))
        return full_segment.startswith(selected) or looks_truncated or selected_letters_only

    def match_answer_texts(self, selected: str, option_candidates: list[str]) -> list[str]:
        selected = self.clean_answer_segment(selected)
        if not selected or not option_candidates:
            return []
        selected_norm = self.normalize_text_for_match(selected)
        found: list[tuple[int, str]] = []
        occupied: list[tuple[int, int]] = []
        for candidate in sorted(option_candidates, key=lambda value: len(self.normalize_text_for_match(value)), reverse=True):
            candidate = candidate.strip()
            candidate_norm = self.normalize_text_for_match(candidate)
            if len(candidate_norm) < 3:
                continue
            exact_pos = selected.find(candidate)
            norm_pos = selected_norm.find(candidate_norm)
            if exact_pos < 0 and norm_pos < 0:
                continue
            pos = exact_pos if exact_pos >= 0 else norm_pos
            end = pos + len(candidate_norm)
            if any(not (end <= start or pos >= stop) for start, stop in occupied):
                continue
            found.append((pos, candidate))
            occupied.append((pos, end))
        return [candidate for _pos, candidate in sorted(found, key=lambda value: value[0])]

    def ocr_option_map(self, details: dict | None) -> dict[str, str]:
        if not details:
            return {}
        try:
            _question, options, _extra_items = self.choice_structure(details.get("items") or [])
        except Exception:
            return {}
        result: dict[str, str] = {}
        for letter in sorted(options):
            text = re.sub(r"\s+", " ", str(options[letter].get("text", ""))).strip()
            result[letter] = text
        return result

    def answer_validation_tokens(self, selected: str) -> list[dict]:
        selected = self.clean_answer_segment(selected)
        if not selected:
            return []
        if "\n" in selected:
            parts = selected.splitlines()
        else:
            parts = re.split(r"[,，;；|/]+", selected)
        tokens: list[dict] = []
        for raw_part in parts:
            part = raw_part.strip(" \t\r\n。；;，,、")
            if not part:
                continue
            compact = re.sub(r"[\s.．。:：、]+", "", part.upper())
            if re.fullmatch(r"[A-F]+", compact):
                for letter in compact:
                    tokens.append({"type": "letter", "value": letter, "raw": part})
                continue
            prefixed = re.match(r"^\s*([A-Fa-f])(?:[.．。、:：)\]】\s]+)(.+)$", part)
            if prefixed:
                tokens.append({"type": "letter", "value": prefixed.group(1).upper(), "raw": part})
                tail = prefixed.group(2).strip(" \t\r\n。；;，,、")
                if tail:
                    tokens.append({"type": "text", "value": tail, "raw": part})
                continue
            normalized = self.normalize_answer_token(part)
            if normalized in {"A", "B", "C", "D", "E", "F"}:
                tokens.append({"type": "letter", "value": normalized, "raw": part})
            elif normalized not in {"正确", "错误"}:
                tokens.append({"type": "text", "value": part, "raw": part})
        return tokens

    def answer_validation_pairs(self, tokens: list[dict]) -> list[dict]:
        pairs: list[dict] = []
        pending_letter = ""
        pending_texts: list[str] = []
        for token in tokens:
            if token.get("type") == "letter":
                if pending_letter:
                    pairs.append({"letter": pending_letter, "text": ",".join(pending_texts), "parts": list(pending_texts)})
                pending_letter = token.get("value", "")
                pending_texts = []
            elif token.get("type") == "text":
                text = str(token.get("value", "")).strip()
                if not text:
                    continue
                if pending_letter:
                    pending_texts.append(text)
                else:
                    pairs.append({"letter": "", "text": text, "parts": [text]})
        if pending_letter:
            pairs.append({"letter": pending_letter, "text": ",".join(pending_texts), "parts": list(pending_texts)})

        letters = [token.get("value", "") for token in tokens if token.get("type") == "letter"]
        texts = [str(token.get("value", "")).strip() for token in tokens if token.get("type") == "text" and str(token.get("value", "")).strip()]
        letter_pairs = [pair for pair in pairs if pair.get("letter")]
        if letters and texts and len(letters) == len(texts):
            needs_repair = len(letter_pairs) != len(letters) or any(not pair.get("text") or len(pair.get("parts") or []) > 1 for pair in letter_pairs)
            if needs_repair:
                pairs = [{"letter": letter, "text": text, "parts": [text]} for letter, text in zip(letters, texts)]
        return pairs

    def answer_letter_prefix(self, selected: str) -> tuple[list[str], str]:
        selected = self.clean_answer_segment(selected)
        match = re.match(r"^\s*((?:[A-Fa-f]\s*[,，、;；|/]\s*)+[A-Fa-f])(?:\s*[,，、;；|/]\s*|\s+)(.+)$", selected, flags=re.DOTALL)
        if not match:
            return [], ""
        letters = []
        for part in re.split(r"[,，、;；|/]+", match.group(1)):
            normalized = self.normalize_answer_token(part)
            if normalized in {"A", "B", "C", "D", "E", "F"} and normalized not in letters:
                letters.append(normalized)
        return letters, match.group(2).strip()

    def option_letters_for_answer_text(self, text: str, option_map: dict[str, str]) -> list[str]:
        normalized = self.normalize_text_for_match(text)
        if len(normalized) < 2:
            return []
        matches = []
        for letter, option_text in option_map.items():
            if option_text and self.text_matches(text, option_text):
                matches.append(letter)
        return matches

    def choice_letters_for_validation(self, choices: list[str], option_map: dict[str, str]) -> list[str]:
        letters: list[str] = []
        for choice in choices or []:
            normalized = self.normalize_answer_token(choice)
            if normalized in option_map:
                if normalized not in letters:
                    letters.append(normalized)
                continue
            matched_letters = self.option_letters_for_answer_text(str(choice), option_map)
            if len(matched_letters) == 1 and matched_letters[0] not in letters:
                letters.append(matched_letters[0])
        return letters

    def validate_answer_against_options(self, selected: str, choices: list[str], ocr_details: dict | None) -> dict:
        selected = self.clean_answer_segment(selected)
        option_map = self.ocr_option_map(ocr_details)
        validation = {
            "ok": True,
            "status": "pass",
            "reason": "答案文本与 OCR 选项一致",
            "selected": selected,
            "choices": choices or [],
            "option_map": option_map,
            "letter_tokens": [],
            "letter_text_pairs": [],
            "choice_letters": [],
            "text_matches": [],
            "mismatches": [],
        }
        judgement_choices = [self.normalize_answer_token(choice) for choice in choices or []]
        if any(choice in {"正确", "错误"} for choice in judgement_choices):
            validation.update(status="skip_judgement", reason="判断题答案跳过选项文本校验")
            return validation
        if not option_map:
            validation.update(status="skip_no_options", reason="OCR 未识别到可校验的选项")
            return validation

        tokens = self.answer_validation_tokens(selected)
        letter_tokens = [token["value"] for token in tokens if token.get("type") == "letter"]
        text_tokens = [token["value"] for token in tokens if token.get("type") == "text"]
        validation["letter_tokens"] = letter_tokens
        choice_letters = self.choice_letters_for_validation(choices or [], option_map)
        validation["choice_letters"] = choice_letters
        letter_text_pairs = self.answer_validation_pairs(tokens)
        validation["letter_text_pairs"] = letter_text_pairs

        text_rows = []
        prefix_letters, prefix_text = self.answer_letter_prefix(selected)
        expected_prefix_letters = choice_letters or prefix_letters
        use_prefix_text_block = bool(prefix_letters and prefix_text and set(prefix_letters) == set(expected_prefix_letters))
        if use_prefix_text_block:
            validation["answer_text_mode"] = "letter_prefix_text_block"
            for letter in expected_prefix_letters:
                option_text = option_map.get(letter, "")
                text_rows.append(
                    {
                        "text": option_text or f"选项 {letter}",
                        "expected_letter": letter,
                        "letters": [letter],
                        "letter": letter,
                        "option": f"{letter}. {option_text}" if option_text else "",
                        "note": "字母列表已选择；后续文本按摘要/改写处理",
                    }
                )
            extra_letters = []
            for letter, option_text in option_map.items():
                if letter in expected_prefix_letters:
                    continue
                if option_text and self.text_matches(option_text, prefix_text):
                    extra_letters.append(letter)
                    text_rows.append(
                        {
                            "text": option_text,
                            "expected_letter": "",
                            "letters": [letter],
                            "letter": letter,
                            "option": f"{letter}. {option_text}",
                        }
                    )
            if extra_letters:
                validation["extra_text_letters"] = extra_letters
            missing_expected = [row for row in text_rows if row.get("expected_letter") and not row.get("letter")]
            if missing_expected and len(text_tokens) == len(expected_prefix_letters):
                text_rows = []
                for letter, text in zip(expected_prefix_letters, text_tokens):
                    letters = self.option_letters_for_answer_text(text, option_map)
                    text_rows.append(
                        {
                            "text": text,
                            "expected_letter": letter,
                            "letters": letters,
                            "letter": letters[0] if len(letters) == 1 else "",
                            "option": f"{letters[0]}. {option_map.get(letters[0], '')}" if len(letters) == 1 else "",
                        }
                    )
        else:
            validation["answer_text_mode"] = "letter_text_pairs"
        letter_pairs = [pair for pair in letter_text_pairs if pair.get("letter")]
        if not use_prefix_text_block and letter_pairs:
            for pair in letter_pairs:
                text = pair.get("text", "")
                if not text:
                    continue
                letters = self.option_letters_for_answer_text(text, option_map)
                text_rows.append(
                    {
                        "text": text,
                        "expected_letter": pair.get("letter", ""),
                        "letters": letters,
                        "letter": letters[0] if len(letters) == 1 else "",
                        "option": f"{letters[0]}. {option_map.get(letters[0], '')}" if len(letters) == 1 else "",
                    }
                )
        elif not use_prefix_text_block:
            option_texts = [text for text in option_map.values() if text]
            matched_texts = self.match_answer_texts(selected, option_texts)
            if matched_texts:
                for text in matched_texts:
                    letters = self.option_letters_for_answer_text(text, option_map)
                    text_rows.append(
                        {
                            "text": text,
                            "expected_letter": "",
                            "letters": letters,
                            "letter": letters[0] if len(letters) == 1 else "",
                            "option": f"{letters[0]}. {option_map.get(letters[0], '')}" if len(letters) == 1 else "",
                        }
                    )
            matched_norms = [self.normalize_text_for_match(row["text"]) for row in text_rows]
            seen_text_keys = set(matched_norms)
            for text in text_tokens:
                key = self.normalize_text_for_match(text)
                if not key or key in seen_text_keys:
                    continue
                if any(key in matched_norm or matched_norm in key for matched_norm in matched_norms):
                    continue
                seen_text_keys.add(key)
                letters = self.option_letters_for_answer_text(text, option_map)
                text_rows.append(
                    {
                        "text": text,
                        "expected_letter": "",
                        "letters": letters,
                        "letter": letters[0] if len(letters) == 1 else "",
                        "option": f"{letters[0]}. {option_map.get(letters[0], '')}" if len(letters) == 1 else "",
                    }
                )
        validation["text_matches"] = text_rows

        mismatches = []
        for row in text_rows:
            if not row["letters"]:
                if row.get("expected_letter"):
                    mismatches.append(f"答案文本中没有找到选项 {row['expected_letter']} 的 OCR 文本“{row['text']}”")
                else:
                    mismatches.append(f"答案文本“{row['text']}”没有命中任何 OCR 选项")
            elif len(row["letters"]) > 1:
                mismatches.append(f"答案文本“{row['text']}”同时匹配多个选项：{','.join(row['letters'])}")
            elif row.get("expected_letter") and row.get("expected_letter") != row.get("letter"):
                mismatches.append(f"字母 {row['expected_letter']} 对应的文本“{row['text']}”实际匹配到选项 {row['letter']}")
            elif not row.get("expected_letter") and row.get("letter") in validation.get("extra_text_letters", []):
                mismatches.append(f"答案文本中出现了未选择的 OCR 选项 {row['letter']}：{row['text']}")

        matched_letters = [row["letter"] for row in text_rows if row.get("letter")]
        if text_rows and not mismatches and choice_letters:
            choice_set = set(choice_letters)
            matched_set = set(matched_letters)
            if matched_set and choice_set != matched_set:
                mismatches.append(f"解析结果集合 {','.join(sorted(choice_set))} 与文本校验集合 {','.join(sorted(matched_set))} 不一致")

        if text_tokens and not text_rows:
            mismatches.append("AI 返回了答案文本，但这些文本都没有匹配到 OCR 选项")

        if mismatches:
            validation.update(ok=False, status="fail", reason=mismatches[0], mismatches=mismatches)
        elif not text_rows:
            validation.update(status="skip_no_text", reason="AI 答案里没有可用于校验的选项文本")
        return validation

    def validation_option_choices(self, validation: dict, fallback_choices: list[str]) -> list[str]:
        option_map = validation.get("option_map") or {}
        letters: list[str] = []
        for row in validation.get("text_matches") or []:
            candidates = []
            if row.get("letter"):
                candidates.append(row.get("letter"))
            candidates.extend(row.get("letters") or [])
            for letter in candidates:
                if letter in option_map and letter not in letters:
                    letters.append(letter)
        if letters:
            return letters
        return list(fallback_choices or [])

    def parse_ai_choices(self, text: str, pattern: str, ocr_details: dict | None = None) -> tuple[list[str], str]:
        pattern = pattern.strip()
        selected = ""
        if pattern:
            try:
                match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
            except re.error as exc:
                raise RuntimeError(f"正则表达式错误：{exc}") from exc
            if match:
                selected = match.group(1) if match.groups() else match.group(0)
        else:
            selected = text
        if not selected:
            selected = self.extract_answer_segment(text)
        selected = self.clean_answer_segment(selected)
        full_segment = self.extract_answer_segment(text)
        if self.should_use_full_answer_segment(selected, full_segment):
            selected = self.clean_answer_segment(full_segment)
        choices: list[str] = []
        judgement_token = self.first_judgement_token(selected)
        compact = re.sub(r"[\s,，、;；/|]+", "", selected.upper())
        if judgement_token and compact not in {"A", "B", "C", "D", "E", "F"}:
            choices.append(judgement_token)
        elif re.fullmatch(r"[A-F]+", compact):
            for letter in compact:
                if letter not in choices:
                    choices.append(letter)
        if not choices:
            for letter in self.answer_letter_tokens(selected):
                if letter not in choices:
                    choices.append(letter)
        if not choices:
            for answer_text in self.match_answer_texts(selected, self.answer_text_candidates(ocr_details)):
                if answer_text not in choices:
                    choices.append(answer_text)
        if not choices:
            for answer_text in self.split_answer_texts(selected):
                token = self.first_judgement_token(answer_text)
                compact_text = re.sub(r"[\s,，、;；/|]+", "", answer_text.upper())
                if token:
                    answer_text = token
                elif re.fullmatch(r"[A-F]+", compact_text):
                    for letter in compact_text:
                        if letter not in choices:
                            choices.append(letter)
                    continue
                if answer_text and answer_text not in choices:
                    choices.append(answer_text)
        if not choices:
            token = self.first_judgement_token(selected)
            if not token and selected != text:
                token = self.first_judgement_token(text)
            if token:
                choices.append(token)
        return choices, selected.strip()

    def extract_answer_segment(self, text: str) -> str:
        patterns = [
            r"(?:正确答案|答案|选择|选项|应选|判断)[:：\s]*([^\n\r]+)",
            r"(?:为|是)\s*(正确|错误|对|错|√|✓|✔|×|✕|✖|True|False|T|F)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text or "", flags=re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return text or ""

    def first_judgement_token(self, text: str) -> str:
        if not text:
            return ""
        compact = re.sub(r"[\s。．.、,，:：;；（）()【】\[\]<>《》]+", "", text)
        upper = compact.upper()
        if "不正确" in compact or "错误" in compact:
            return "错误"
        if "正确" in compact:
            return "正确"
        if compact in {"错", "否", "假", "×", "✕", "✖"} or upper in {"FALSE", "NO", "F", "N"}:
            return "错误"
        if compact in {"对", "是", "真", "√", "✓", "✔"} or upper in {"TRUE", "YES", "T", "Y"}:
            return "正确"
        return ""

    def wait_until_resumed(self, should_stop=None, should_pause=None) -> bool:
        while should_pause and should_pause():
            if should_stop and should_stop():
                return False
            time.sleep(0.1)
        return True

    def wait_with_cancel(self, seconds: float, should_stop=None, should_pause=None) -> bool:
        remaining = max(0.0, seconds)
        while remaining > 0:
            if should_stop and should_stop():
                return False
            if not self.wait_until_resumed(should_stop, should_pause):
                return False
            chunk = min(0.1, remaining)
            time.sleep(chunk)
            remaining -= chunk
        return True

    def int_param(self, value, default: int = 0) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    def float_param(self, value, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def branch_next_index(self, current_index: int, action_count: int, branch_action: str, branch_value) -> tuple[int, str]:
        if branch_action == "skip":
            skip_count = max(0, self.int_param(branch_value, 1))
            return current_index + skip_count + 1, f"跳过后面 {skip_count} 步"
        if branch_action == "jump":
            step_number = max(1, self.int_param(branch_value, current_index + 2))
            target_index = min(step_number - 1, action_count)
            return target_index, f"跳到第 {step_number} 步"
        if branch_action == "stop":
            return action_count, "停止本轮"
        return current_index + 1, "继续下一步"

    def execute_workflow(
        self,
        actions: list[dict] | None = None,
        should_stop=None,
        should_pause=None,
        on_event=None,
        stop_on_similarity: bool = False,
    ) -> dict:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        actions = normalize_workflow_actions(actions or self.config.workflow_actions)
        context = {
            "q": "",
            "ocr_details": None,
            "ocr_raw_text": "",
            "ocr_search_text": "",
            "ai_answer": "",
            "matches": [],
            "parsed_text": "",
            "screenshot_similarity": None,
            "screenshot_similarity_hit": False,
            "answer_validation": None,
            "answer_validation_failed": False,
            "answer_validation_error": "",
        }
        logs: list[str] = []
        snapshot_path = ""

        def add_log(text: str) -> None:
            logs.append(text)

        def short_value(value, limit: int = 140) -> str:
            if value is None:
                return "-"
            if isinstance(value, (dict, list, tuple)):
                text = json.dumps(value, ensure_ascii=False, default=str)
            else:
                text = str(value)
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) > limit:
                return text[: limit - 1] + "…"
            return text

        def size_text(value) -> str:
            if not value:
                return "-"
            try:
                return f"{value[0]}x{value[1]}"
            except Exception:
                return short_value(value)

        def add_step(index_value: int, label_value: str, status: str, *details) -> None:
            add_log(f"{index_value + 1:02d}. {label_value} [{status}]")
            for detail in details:
                if not detail:
                    continue
                if isinstance(detail, tuple) and len(detail) == 2:
                    key, value = detail
                    add_log(f"=>\t{key}: {short_value(value)}")
                else:
                    add_log(f"=>\t{short_value(detail)}")

        def emit(event: str, **payload) -> None:
            if on_event:
                on_event(event, payload)

        index = 0
        executed_steps = 0
        max_steps = max(100, len(actions) * 20)
        while index < len(actions):
            executed_steps += 1
            if executed_steps > max_steps:
                add_log("00. 流程保护 [stop]")
                add_log(f"=>\t原因: 超过 {max_steps} 个执行步，可能存在循环跳转")
                break
            if should_stop and should_stop():
                add_log("00. 流程停止 [stop]")
                add_log("=>\t原因: 收到停止信号")
                break
            if not self.wait_until_resumed(should_stop, should_pause):
                add_log("00. 流程停止 [stop]")
                add_log("=>\t原因: 暂停等待期间收到停止信号")
                break
            action = actions[index]
            action_type = action["type"]
            label = ACTION_TYPES.get(action_type, action_type)
            params = action.get("params") or {}
            emit("step_start", index=index, action=action)
            if not action.get("enabled", True):
                add_step(index, label, "skip", ("type", action_type), ("enabled", False))
                emit("step_skip", index=index, action=action)
                index += 1
                continue

            if action_type == "ocr":
                force = bool(params.get("force", True))
                details = self.ocr_region_details(force=force)
                context["ocr_details"] = details
                context["q"] = details.get("text", "")
                context["ocr_raw_text"] = details.get("raw_text", "")
                context["ocr_search_text"] = details.get("search_text", "")
                if self.config.save_snapshots and not snapshot_path:
                    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
                    snapshot_path = str(SNAPSHOT_DIR / (time.strftime("paddle_%Y%m%d_%H%M%S") + ".png"))
                    details["image"].save(snapshot_path)
                source = "缓存" if details.get("cache_hit") else "新截图"
                add_step(
                    index,
                    label,
                    "done",
                    ("source", source),
                    ("force", force),
                    ("region", asdict(region) if (region := self.config.region) else "-"),
                    ("image_size", size_text(details["image"].size)),
                    ("items", len(details.get("items", []))),
                    ("snapshot", snapshot_path or "未保存"),
                    ("text_preview", details.get("text", "")[:180] or "(空)"),
                )
                emit("ocr", index=index, action=action, details=details)

            elif action_type == "if_text":
                condition = params.get("condition", "text")
                condition_details = [("condition", condition)]
                if condition == "screenshot_similarity":
                    details = context.get("ocr_details")
                    if not details:
                        details = self.ocr_region_details(force=True)
                        context["ocr_details"] = details
                        context["q"] = details.get("text", "")
                        context["ocr_raw_text"] = details.get("raw_text", "")
                        context["ocr_search_text"] = details.get("search_text", "")
                    threshold = self.float_param(params.get("similarity_threshold", 90), 90.0)
                    lag = self.similarity_lag(params.get("similarity_lag", 1))
                    comparison = self.compare_with_previous_screenshot(details["image"], threshold, lag)
                    context["screenshot_similarity"] = comparison.get("similarity")
                    context["screenshot_similarity_hit"] = bool(comparison.get("hit"))
                    context["screenshot_similarity_debug"] = comparison
                    hit = bool(comparison.get("hit"))
                    if comparison.get("available"):
                        condition_log = f"当前截图 vs {lag} 张前截图：前景相似度 {comparison['similarity']:.1f}% / 阈值 {comparison['threshold']:.1f}%"
                    else:
                        condition_log = comparison.get("reason") or "截图相似度：历史截图不足，已记录当前截图"
                    condition_details.extend(
                        [
                            ("previous_available", bool(comparison.get("available"))),
                            ("lag", comparison.get("lag", lag)),
                            ("current_seq", comparison.get("current_seq")),
                            ("previous_seq", comparison.get("previous_seq")),
                            ("history_count_before", comparison.get("history_count_before")),
                            ("history_count_after", comparison.get("history_count_after")),
                            ("current_size", size_text(comparison.get("current_size"))),
                            ("previous_size", size_text(comparison.get("previous_size"))),
                            ("sample_size", size_text(comparison.get("sample_size"))),
                            ("mean_diff", f"{comparison['mean_diff']:.3f}" if comparison.get("mean_diff") is not None else "-"),
                            ("foreground_pixels", comparison.get("foreground_pixels") if comparison.get("foreground_pixels") is not None else "-"),
                            ("changed_pixels", comparison.get("changed_pixels") if comparison.get("changed_pixels") is not None else "-"),
                            ("changed_ratio", f"{comparison['changed_ratio'] * 100:.2f}%" if comparison.get("changed_ratio") is not None else "-"),
                            ("similarity", f"{comparison['similarity']:.2f}%" if comparison.get("similarity") is not None else "-"),
                            ("legacy_similarity", f"{comparison['legacy_similarity']:.2f}%" if comparison.get("legacy_similarity") is not None else "-"),
                            ("legacy_mean_diff", f"{comparison['legacy_mean_diff']:.3f}" if comparison.get("legacy_mean_diff") is not None else "-"),
                            ("foreground_threshold", comparison.get("foreground_threshold", "-")),
                            ("difference_threshold", comparison.get("difference_threshold", "-")),
                            ("threshold", f"{comparison.get('threshold', threshold):.2f}%"),
                            ("hit", hit),
                            ("reference_updated", bool(comparison.get("reference_updated"))),
                            ("stop_on_similarity", stop_on_similarity),
                        ]
                    )
                    emit("similarity", index=index, action=action, comparison=comparison)
                else:
                    source = params.get("source", "ocr")
                    source_value = self.source_text(source, context)
                    terms = self.split_condition_terms(params.get("text", ""))
                    mode = params.get("mode", "all")
                    hit = bool(terms) and (all(term in source_value for term in terms) if mode == "all" else any(term in source_value for term in terms))
                    condition_log = f"{'满足' if hit else '不满足'} [{', '.join(terms) or '空'}]"
                    condition_details.extend(
                        [
                            ("source", source),
                            ("mode", mode),
                            ("terms", terms or []),
                            ("source_len", len(source_value)),
                            ("source_preview", source_value[:180] or "(空)"),
                            ("hit", hit),
                        ]
                    )
                branch_action = params.get("true_action" if hit else "false_action", "continue" if hit else "skip")
                branch_value = params.get("true_value" if hit else "false_value", "1")
                next_index, branch_log = self.branch_next_index(index, len(actions), branch_action, branch_value)
                condition_details.extend(
                    [
                        ("result", "true" if hit else "false"),
                        ("summary", condition_log),
                        ("branch_action", branch_action),
                        ("branch_value", branch_value),
                        ("next_step", "结束本轮" if next_index >= len(actions) else next_index + 1),
                        ("branch_summary", branch_log),
                    ]
                )
                add_step(index, label, "hit" if hit else "miss", *condition_details)
                emit("step_done", index=index, action=action)
                if condition == "screenshot_similarity" and hit and stop_on_similarity:
                    add_log("=>\tbatch_action: 截图相似命中，当前网址完成，准备跳转到下一个网址")
                    break
                index = next_index
                continue

            elif action_type == "click_text":
                target = self.render_template(params.get("text", ""), context).strip()
                click_area = params.get("click_area", "letter")
                result = self.locate_target(target, click_area=click_area) if target else None
                if result:
                    self.click_position(*result["pos"])
                    add_step(
                        index,
                        label,
                        "clicked",
                        ("target", target),
                        ("click_area", click_area),
                        ("pos", f"{result['pos'][0]}, {result['pos'][1]}"),
                        ("matched_label", result.get("label", "")),
                        ("item_text", (result.get("item") or {}).get("text", "")),
                        ("cache_hit", bool(result.get("cache_hit"))),
                    )
                    emit("located", index=index, action=action, result=result, title=f"流程点击文字：{target}")
                else:
                    add_step(index, label, "miss", ("target", target or "(空)"), ("click_area", click_area), ("reason", "未找到目标文字"))

            elif action_type == "wait":
                seconds = max(0.0, self.float_param(params.get("seconds", 1), 1.0))
                add_step(index, label, "start", ("seconds", f"{seconds:g}"))
                if not self.wait_with_cancel(seconds, should_stop, should_pause):
                    add_log("=>\tresult: 等待被停止")
                    break
                add_log("=>\tresult: 等待完成")

            elif action_type == "ask_ai":
                if not context.get("q"):
                    details = self.ocr_region_details(force=False)
                    context["ocr_details"] = details
                    context["q"] = details.get("text", "")
                    context["ocr_raw_text"] = details.get("raw_text", "")
                    context["ocr_search_text"] = details.get("search_text", "")
                template = params.get("prompt") or "{U}\n\nQ:\n{Q}"
                prompt = self.render_template(template, context)
                include_image = bool_value(params.get("include_image", False), False)
                image = (context.get("ocr_details") or {}).get("image") if include_image else None
                result = self.post_generate_content(prompt, image=image)
                if not result["ok"]:
                    raise RuntimeError(f"API 请求失败：HTTP {result['http_status'] or '-'}\n{result['error'] or result['raw']}")
                answer = self.extract_answer(result["payload"] or {})
                context["ai_answer"] = answer
                add_step(
                    index,
                    label,
                    "done",
                    ("prompt_len", len(prompt)),
                    ("prompt_preview", prompt[:180]),
                    ("include_image", bool(image)),
                    ("image_size", size_text(image.size) if image else "-"),
                    ("http_status", result.get("http_status")),
                    ("elapsed_ms", result.get("elapsed_ms")),
                    ("answer_preview", answer[:180] or "(空)"),
                )

            elif action_type == "parse_ai":
                source = params.get("source", "ai")
                source_value = self.source_text(source, context)
                choices, parsed_text = self.parse_ai_choices(source_value, params.get("regex", ""), context.get("ocr_details"))
                validation = self.validate_answer_against_options(parsed_text, choices, context.get("ocr_details"))
                validation_pause = bool(getattr(self.config, "answer_validation_pause_on_mismatch", False))
                if not validation.get("ok", True) and not validation_pause:
                    override_choices = self.validation_option_choices(validation, choices)
                    if override_choices:
                        choices = override_choices
                        validation["override_applied"] = True
                        validation["override_choices"] = override_choices
                        validation["status"] = "override"
                        validation["reason"] = f"校验不一致，已按配置不暂停，使用文本/选项结果：{','.join(override_choices)}"
                context["matches"] = choices
                context["parsed_text"] = parsed_text
                context["answer_validation"] = validation
                validation_matches = [
                    f"{(row.get('expected_letter') + ': ') if row.get('expected_letter') else ''}{row.get('text', '')} -> {row.get('letter') or ','.join(row.get('letters') or []) or '未命中'}{(' / ' + row.get('note')) if row.get('note') else ''}"
                    for row in validation.get("text_matches", [])
                ]
                parse_details = [
                    ("source", source),
                    ("regex", params.get("regex", "")),
                    ("source_preview", source_value[:180] or "(空)"),
                    ("parsed_text", parsed_text or "(空)"),
                    ("matches", choices or []),
                    ("answer_validation", f"{validation.get('status')}: {validation.get('reason')}"),
                    ("validation_pause_on_mismatch", validation_pause),
                    ("validation_override_choices", validation.get("override_choices") or []),
                    ("validation_letters", validation.get("choice_letters") or validation.get("letter_tokens") or []),
                    ("validation_text_matches", validation_matches or []),
                    ("ocr_options", [f"{letter}. {text}" for letter, text in validation.get("option_map", {}).items()]),
                ]
                if not validation.get("ok", True) and validation_pause:
                    context["answer_validation_failed"] = True
                    context["answer_validation_error"] = validation.get("reason", "答案文本校验失败")
                    add_step(
                        index,
                        label,
                        "error",
                        *parse_details,
                        ("validation_mismatches", validation.get("mismatches") or []),
                    )
                    emit("answer_validation_failed", index=index, action=action, validation=validation)
                    break
                add_step(index, label, "done" if choices else "miss", *parse_details)

            elif action_type == "click_matches":
                choices = context.get("matches") or []
                click_area = params.get("click_area", "letter")
                delay = max(0.0, self.float_param(params.get("delay", 0.2), 0.2))
                click_details = [("matches", choices or []), ("click_area", click_area), ("delay", f"{delay:g}")]
                if not choices:
                    add_step(index, label, "skip", *click_details, ("reason", "没有可点击结果"))
                for choice in choices:
                    result = self.locate_choice(choice, click_area=click_area)
                    if result:
                        self.click_position(*result["pos"])
                        click_details.append(("clicked", f"{choice} -> {result['pos'][0]}, {result['pos'][1]} / {result.get('label', '')}"))
                        emit("located", index=index, action=action, result=result, title=f"流程点击选项 {choice}")
                        if delay:
                            if not self.wait_with_cancel(delay, should_stop, should_pause):
                                click_details.append(("delay_result", "点击间隔等待被停止"))
                                break
                    else:
                        click_details.append(("miss", choice))
                if choices:
                    clicked_count = len([item for item in click_details if isinstance(item, tuple) and item[0] == "clicked"])
                    add_step(index, label, "done" if clicked_count else "miss", *click_details, ("clicked_count", clicked_count))

            emit("step_done", index=index, action=action)
            index += 1

        record = {
            "time": timestamp,
            "engine": "paddleocr",
            "region": asdict(self.config.region),
            "q": context.get("q", ""),
            "answer": context.get("ai_answer", ""),
            "matches": context.get("matches", []),
            "answer_validation_failed": bool(context.get("answer_validation_failed")),
            "answer_validation_error": context.get("answer_validation_error", ""),
            "answer_validation_debug": context.get("answer_validation"),
            "screenshot_similarity": context.get("screenshot_similarity"),
            "screenshot_similarity_hit": bool(context.get("screenshot_similarity_hit")),
            "screenshot_similarity_debug": context.get("screenshot_similarity_debug"),
            "workflow_logs": logs,
            "snapshot": snapshot_path,
        }
        RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with RESULTS_PATH.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def api_key(self) -> str:
        provider = self.api_provider()
        api_key = self.config.api_key.strip()
        env_names = [self.config.api_key_env]
        if provider == "openai":
            env_names.append("OPENAI_API_KEY")
        else:
            env_names.append("SUB2API_API_KEY")
        for env_name in env_names:
            if api_key or not env_name:
                continue
            api_key = os.getenv(env_name, "").strip()
        if not api_key:
            names = " / ".join(dict.fromkeys(name for name in env_names if name))
            raise RuntimeError(f"没有 API Key，请设置环境变量 {names} 或在界面中填写。")
        return api_key

    def api_provider(self) -> str:
        provider = getattr(self.config, "api_provider", "gemini")
        if provider in {"gemini", "openai"}:
            return provider
        base_url = self.config.api_base_url.strip().rstrip("/")
        model = self.config.gemini_model.strip().lower()
        if base_url.endswith("/v1") or model.startswith("gpt-"):
            return "openai"
        return "gemini"

    def post_generate_content(self, prompt: str, image: Image.Image | None = None, timeout: int = 90) -> dict:
        if self.api_provider() == "openai":
            return self.post_openai_compatible(prompt, image=image, timeout=timeout)
        return self.post_gemini_generate_content(prompt, image=image, timeout=timeout)

    def post_gemini_generate_content(self, prompt: str, image: Image.Image | None = None, timeout: int = 90) -> dict:
        api_key = self.api_key()
        base_url = self.config.api_base_url.strip().rstrip("/")
        url = f"{base_url}/models/{self.config.gemini_model}:generateContent"
        parts = [{"text": prompt}]
        if image is not None:
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            parts.append({"inline_data": {"mime_type": "image/png", "data": base64.b64encode(buffer.getvalue()).decode("ascii")}})
        body = {"contents": [{"role": "user", "parts": parts}], "generationConfig": {"temperature": 0}}
        headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}
        if "generativelanguage.googleapis.com" not in base_url:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
        started_at = time.perf_counter()
        result = {"ok": False, "url": url, "model": self.config.gemini_model, "http_status": None, "elapsed_ms": None, "payload": None, "raw": "", "error": ""}
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                result.update(ok=True, http_status=response.status, raw=raw, payload=json.loads(raw))
        except urllib.error.HTTPError as exc:
            result["http_status"] = exc.code
            result["error"] = exc.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as exc:
            result["error"] = f"网络错误：{exc}"
        finally:
            result["elapsed_ms"] = round((time.perf_counter() - started_at) * 1000)
        return result

    def image_data_url(self, image: Image.Image) -> str:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")

    def post_openai_compatible(self, prompt: str, image: Image.Image | None = None, timeout: int = 90) -> dict:
        api_key = self.api_key()
        base_url = self.config.api_base_url.strip().rstrip("/")
        url = f"{base_url}/chat/completions"
        content: str | list[dict] = prompt
        if image is not None:
            content = [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": self.image_data_url(image)}},
            ]
        body = {
            "model": self.config.gemini_model.strip() or DEFAULT_OPENAI_MODEL,
            "messages": [{"role": "user", "content": content}],
        }
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
        request = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
        started_at = time.perf_counter()
        result = {
            "ok": False,
            "provider": "openai",
            "url": url,
            "model": body["model"],
            "http_status": None,
            "elapsed_ms": None,
            "payload": None,
            "raw": "",
            "error": "",
        }
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                result.update(ok=True, http_status=response.status, raw=raw, payload=json.loads(raw))
        except urllib.error.HTTPError as exc:
            result["http_status"] = exc.code
            result["error"] = exc.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as exc:
            result["error"] = f"网络错误：{exc}"
        finally:
            result["elapsed_ms"] = round((time.perf_counter() - started_at) * 1000)
        return result

    def extract_answer(self, payload: dict) -> str:
        if "choices" in payload:
            choices = payload.get("choices") or []
            if not choices:
                return ""
            message = choices[0].get("message") or {}
            content = message.get("content", "")
            if isinstance(content, list):
                parts = []
                for part in content:
                    if isinstance(part, dict):
                        parts.append(str(part.get("text") or part.get("content") or ""))
                    else:
                        parts.append(str(part))
                return "".join(parts).strip()
            return str(content or choices[0].get("text", "")).strip()
        if "output_text" in payload:
            return str(payload.get("output_text") or "").strip()
        if "output" in payload:
            parts = []
            for item in payload.get("output") or []:
                for part in item.get("content") or []:
                    if isinstance(part, dict):
                        parts.append(str(part.get("text") or ""))
            return "".join(parts).strip()
        parts = payload.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        return "".join(part.get("text", "") for part in parts).strip()

    def ask_gemini(self, q_text: str) -> str:
        prompt = f"{self.config.prompt_u.strip()}\n\nQ:\n{q_text.strip()}"
        result = self.post_generate_content(prompt)
        if not result["ok"]:
            raise RuntimeError(f"API 请求失败：HTTP {result['http_status'] or '-'}\n{result['error'] or result['raw']}")
        return self.extract_answer(result["payload"] or {})

    def test_ai_connectivity(self) -> dict:
        result = self.post_generate_content("连通性测试：请只回复 OK。", timeout=30)
        if result["ok"]:
            result["answer"] = self.extract_answer(result["payload"] or {})
        return result

    def run_once(self) -> dict:
        return self.execute_workflow(self.config.workflow_actions)


class ScrollableFrame(ttk.Frame):
    def __init__(self, parent, padding=10):
        super().__init__(parent)
        self.canvas = Canvas(self, borderwidth=0, highlightthickness=0, background="#f8fafc")
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.frame = ttk.Frame(self.canvas, padding=padding)
        self.window_id = self.canvas.create_window((0, 0), window=self.frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side=LEFT, fill=BOTH, expand=True)
        self.scrollbar.pack(side=RIGHT, fill="y")
        self.frame.bind("<Configure>", self._on_frame_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Enter>", self._bind_mousewheel)
        self.canvas.bind("<Leave>", self._unbind_mousewheel)
        self.frame.bind("<Enter>", self._bind_mousewheel)
        self.frame.bind("<Leave>", self._unbind_mousewheel)

    def _on_frame_configure(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event) -> None:
        self.canvas.itemconfigure(self.window_id, width=event.width)

    def _bind_mousewheel(self, _event=None) -> None:
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _unbind_mousewheel(self, _event=None) -> None:
        self.canvas.unbind_all("<MouseWheel>")

    def _on_mousewheel(self, event) -> None:
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")


class PaddleApp:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title("屏幕 OCR + AI - PaddleOCR 版")
        self.config = load_config()
        self.worker = PaddleWorker(self.config)
        self.running = False
        self.busy = False
        self.paused = False
        self.validation_pause_pending = False
        self.settings_collapsed = False
        self.pause_event = threading.Event()
        self.pause_event.set()
        self.stop_event = threading.Event()
        self.preview_photo = None
        self.region_corner_points: dict[str, tuple[int, int]] = {}

        self.interval_var = StringVar(value=str(self.config.interval_seconds))
        self.base_url_var = StringVar(value=self.config.api_base_url)
        self.model_var = StringVar(value=self.config.gemini_model)
        self.api_key_var = StringVar(value=self.config.api_key)
        self.lang_var = StringVar(value=self.config.paddle_lang)
        self.choice_formatter_var = BooleanVar(value=self.config.use_choice_formatter)
        self.save_snapshots_var = BooleanVar(value=self.config.save_snapshots)
        self.answer_validation_pause_var = BooleanVar(value=getattr(self.config, "answer_validation_pause_on_mismatch", False))
        self.browser_enabled_var = BooleanVar(value=self.config.browser_enabled)
        self.browser_path_var = StringVar(value=self.config.browser_path)
        self.browser_debug_port_var = StringVar(value=str(self.config.browser_debug_port))
        self.browser_window_vars = {
            "x": StringVar(value=str(self.config.browser_window_x)),
            "y": StringVar(value=str(self.config.browser_window_y)),
            "w": StringVar(value=str(self.config.browser_window_w)),
            "h": StringVar(value=str(self.config.browser_window_h)),
        }
        self.browser_wait_seconds_var = StringVar(value=str(self.config.browser_wait_seconds))
        self.browser_next_wait_seconds_var = StringVar(value=str(self.config.browser_next_wait_seconds))
        self.batch_status_var = StringVar(value="未启用网址批处理")
        self.region_vars = {
            "x": StringVar(value=str(self.config.region.x)),
            "y": StringVar(value=str(self.config.region.y)),
            "w": StringVar(value=str(self.config.region.w)),
            "h": StringVar(value=str(self.config.region.h)),
        }
        self.status_var = StringVar(value="PaddleOCR 就绪，首次 OCR 会加载模型")
        self.find_text_var = StringVar()
        self.position_var = StringVar(value="未定位")
        self.api_provider_var = StringVar(value=API_PROVIDER_LABELS.get(getattr(self.config, "api_provider", "gemini"), "Gemini / Sub2API v1beta"))
        self.workflow_actions = copy.deepcopy(normalize_workflow_actions(self.config.workflow_actions))
        self.action_type_var = StringVar(value=ACTION_TYPE_OPTIONS[0])
        self.action_enabled_var = BooleanVar(value=True)
        self.action_condition_var = StringVar(value="文本包含")
        self.action_text_var = StringVar()
        self.action_seconds_var = StringVar(value="1")
        self.action_skip_var = StringVar(value="1")
        self.action_regex_var = StringVar(value=DEFAULT_PARSE_REGEX)
        self.action_source_var = StringVar(value="OCR 文本")
        self.action_mode_var = StringVar(value="全部包含")
        self.action_similarity_threshold_var = StringVar(value="90")
        self.action_similarity_lag_var = StringVar(value="1")
        self.action_click_area_var = StringVar(value="选项圆圈")
        self.action_delay_var = StringVar(value="0.2")
        self.action_include_image_var = BooleanVar(value=False)
        self.action_true_branch_var = StringVar(value="继续下一步")
        self.action_true_value_var = StringVar(value="1")
        self.action_false_branch_var = StringVar(value="跳过后面 N 步")
        self.action_false_value_var = StringVar(value="1")
        self.action_hint_var = StringVar(value="")
        self.action_title_var = StringVar(value="未选择动作")
        self.current_action_index: int | None = None
        self.executing_step_index: int | None = None
        self.browser_controller: BrowserController | None = None
        self.browser_url_index = 0
        self.current_browser_url = ""
        self.debug_photos: list[ImageTk.PhotoImage] = []
        self._loading_action_editor = False
        self.build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def build_ui(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        bg = "#f8fafc"
        surface = "#ffffff"
        muted = "#64748b"
        text = "#0f172a"
        border = "#dbe3ef"
        accent = "#2563eb"
        self.root.configure(bg=bg)
        self.root.option_add("*TCombobox*Listbox.font", ("Microsoft YaHei UI", 10))
        style.configure(".", font=("Microsoft YaHei UI", 10))
        style.configure("TFrame", background=bg)
        style.configure("Surface.TFrame", background=surface)
        style.configure("Toolbar.TFrame", background=surface)
        style.configure("TLabel", background=bg, foreground=text)
        style.configure("Surface.TLabel", background=surface, foreground=text)
        style.configure("Muted.TLabel", background=surface, foreground=muted)
        style.configure("Status.TLabel", background=surface, foreground=muted)
        style.configure("TButton", padding=(11, 7), relief="flat", background="#eef2f7", foreground=text, bordercolor="#e2e8f0")
        style.map("TButton", background=[("active", "#e2e8f0"), ("pressed", "#cbd5e1")])
        style.configure("Accent.TButton", padding=(13, 8), foreground="#ffffff", background=accent, bordercolor=accent)
        style.map("Accent.TButton", background=[("active", "#1d4ed8"), ("pressed", "#1e40af")], foreground=[("disabled", "#e5e7eb")])
        style.configure("TLabelframe", background=surface, bordercolor=border, relief="solid")
        style.configure("TLabelframe.Label", background=surface, foreground=text, font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("TNotebook", background=surface, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(16, 8), background="#eef2f7", foreground="#475569")
        style.map(
            "TNotebook.Tab",
            background=[("selected", surface), ("active", "#e2e8f0")],
            foreground=[("selected", text)],
            padding=[("selected", (16, 8)), ("active", (16, 8))],
            expand=[("selected", (0, 0, 0, 0))],
        )
        style.configure("Treeview", rowheight=28, font=("Microsoft YaHei UI", 9), background=surface, fieldbackground=surface, foreground=text, bordercolor=border)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"), background="#eef2f7", foreground=text)
        style.configure("TPanedwindow", background=bg)

        root_frame = ttk.Frame(self.root, padding=(14, 12))
        root_frame.pack(fill=BOTH, expand=True)

        toolbar = ttk.Frame(root_frame, style="Toolbar.TFrame", padding=(12, 10))
        toolbar.pack(fill="x", pady=(0, 12))
        toolbar_row_1 = ttk.Frame(toolbar, style="Toolbar.TFrame")
        toolbar_row_1.pack(fill="x")
        toolbar_row_2 = ttk.Frame(toolbar, style="Toolbar.TFrame")
        toolbar_row_2.pack(fill="x", pady=(8, 0))

        ttk.Button(toolbar_row_1, text="保存", command=self.save_from_ui).pack(side=LEFT)
        ttk.Button(toolbar_row_1, text="测试AI", command=self.test_ai_async).pack(side=LEFT, padx=(8, 0))
        ttk.Button(toolbar_row_1, text="执行一次", command=self.run_once_async, style="Accent.TButton").pack(side=LEFT, padx=(8, 0))
        ttk.Button(toolbar_row_1, text="刷新OCR", command=self.refresh_ocr_cache_async).pack(side=LEFT, padx=(8, 0))
        ttk.Button(toolbar_row_1, text="OCR视图", command=self.visualize_ocr_async).pack(side=LEFT, padx=(8, 0))
        self.collapse_button = ttk.Button(toolbar_row_1, text="收起设置", command=self.toggle_settings_panel)
        self.collapse_button.pack(side=LEFT, padx=(8, 0))

        self.start_button = ttk.Button(toolbar_row_2, text="开始", command=self.toggle_loop)
        self.start_button.pack(side=LEFT)
        self.pause_button = ttk.Button(toolbar_row_2, text="暂停", command=self.toggle_pause)
        self.pause_button.pack(side=LEFT, padx=(8, 0))
        self.stop_button = ttk.Button(toolbar_row_2, text="停止", command=self.stop_loop)
        self.stop_button.pack(side=LEFT, padx=(8, 0))
        ttk.Label(toolbar_row_2, textvariable=self.status_var, style="Status.TLabel", wraplength=620, justify="right").pack(side=RIGHT, fill="x", expand=True, padx=(12, 0))

        main_pane = ttk.PanedWindow(root_frame, orient="vertical")
        main_pane.pack(fill=BOTH, expand=True)
        self.main_pane = main_pane

        content = ttk.PanedWindow(main_pane, orient="horizontal")
        main_pane.add(content, weight=5)
        self.content_pane = content

        preview_box = ttk.LabelFrame(content, text="目标区域预览", padding=10)
        content.add(preview_box, weight=3)
        self.preview_label = ttk.Label(
            preview_box,
            text="点击“截图预览”或“OCR 可视化”查看目标区域",
            anchor="center",
            justify="center",
            font=("Microsoft YaHei UI", 11),
            foreground="#64748b",
            style="Muted.TLabel",
        )
        self.preview_label.pack(fill=BOTH, expand=True)

        controls = ttk.Notebook(content)
        content.add(controls, weight=2)
        run_scroll = ScrollableFrame(controls, padding=10)
        browser_scroll = ScrollableFrame(controls, padding=10)
        workflow_scroll = ScrollableFrame(controls, padding=10)
        region_scroll = ScrollableFrame(controls, padding=10)
        tool_scroll = ScrollableFrame(controls, padding=10)
        debug_scroll = ScrollableFrame(controls, padding=10)
        controls.add(run_scroll, text="运行")
        controls.add(browser_scroll, text="浏览器")
        controls.add(workflow_scroll, text="流程")
        controls.add(region_scroll, text="区域")
        controls.add(tool_scroll, text="工具")
        controls.add(debug_scroll, text="调试")
        run_tab = run_scroll.frame
        browser_tab = browser_scroll.frame
        workflow_tab = workflow_scroll.frame
        region_tab = region_scroll.frame
        tool_tab = tool_scroll.frame
        debug_tab = debug_scroll.frame

        self.add_entry(run_tab, "间隔秒数", self.interval_var)
        provider_row = ttk.Frame(run_tab)
        provider_row.pack(fill="x", pady=4)
        ttk.Label(provider_row, text="接口类型").pack(anchor="w")
        provider_combo = ttk.Combobox(provider_row, textvariable=self.api_provider_var, values=list(API_PROVIDER_OPTIONS), state="readonly")
        provider_combo.pack(fill="x")
        provider_combo.bind("<<ComboboxSelected>>", self.on_api_provider_changed)
        self.add_entry(run_tab, "请求地址", self.base_url_var)
        model_row = ttk.Frame(run_tab)
        model_row.pack(fill="x", pady=4)
        ttk.Label(model_row, text="模型").pack(anchor="w")
        ttk.Combobox(model_row, textvariable=self.model_var, values=MODEL_PRESETS).pack(fill="x")
        self.add_entry(run_tab, "API Key", self.api_key_var, show="*")
        prompt_box = ttk.LabelFrame(run_tab, text="提示语 U", padding=10)
        prompt_box.pack(fill=BOTH, expand=True, pady=(10, 0))
        self.prompt_input = Text(
            prompt_box,
            height=8,
            width=34,
            wrap="word",
            bg="#ffffff",
            fg="#0f172a",
            insertbackground="#2563eb",
            relief="flat",
            padx=8,
            pady=8,
            font=("Microsoft YaHei UI", 10),
        )
        self.prompt_input.insert("1.0", self.config.prompt_u)
        self.prompt_input.pack(fill=BOTH, expand=True)

        self.build_browser_tab(browser_tab)
        self.build_workflow_tab(workflow_tab)
        self.build_debug_tab(debug_tab)

        coord_box = ttk.LabelFrame(region_tab, text="目标区域坐标", padding=8)
        coord_box.pack(fill="x")
        for col, key in enumerate(("x", "y", "w", "h")):
            ttk.Label(coord_box, text=key.upper()).grid(row=0, column=col, padx=4, sticky="w")
            ttk.Entry(coord_box, textvariable=self.region_vars[key], width=9).grid(row=1, column=col, padx=4, sticky="ew")
            coord_box.columnconfigure(col, weight=1)
        ttk.Button(region_tab, text="拖拽选择区域", command=self.pick_region).pack(fill="x", pady=(10, 0))
        corner_buttons = ttk.Frame(region_tab)
        corner_buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(corner_buttons, text="3秒后取左上角", command=lambda: self.capture_region_corner_after_delay("tl")).pack(side=LEFT, fill="x", expand=True)
        ttk.Button(corner_buttons, text="3秒后取右下角", command=lambda: self.capture_region_corner_after_delay("br")).pack(side=RIGHT, fill="x", expand=True, padx=(8, 0))
        ttk.Button(region_tab, text="截图预览", command=self.preview_region).pack(fill="x", pady=(8, 0))
        ttk.Button(region_tab, text="OCR 可视化窗口", command=self.visualize_ocr_async).pack(fill="x", pady=(8, 0))

        self.add_entry(tool_tab, "PaddleOCR 语言", self.lang_var)
        ttk.Checkbutton(tool_tab, text="选择题格式化 A/B/C/D", variable=self.choice_formatter_var).pack(anchor="w", pady=(8, 0))
        ttk.Checkbutton(tool_tab, text="保存每轮截图到 runtime/snapshots", variable=self.save_snapshots_var).pack(anchor="w", pady=(6, 0))
        ttk.Checkbutton(tool_tab, text="答案校验不一致时暂停", variable=self.answer_validation_pause_var).pack(anchor="w", pady=(6, 0))
        ttk.Label(tool_tab, text="关闭时遇到校验冲突会继续执行，并优先使用文本/选项校验出的结果。", style="Muted.TLabel", wraplength=360, justify="left").pack(fill="x", pady=(2, 0))
        locate_box = ttk.LabelFrame(tool_tab, text="文字定位/点击辅助", padding=8)
        locate_box.pack(fill="x", pady=(12, 0))
        ttk.Entry(locate_box, textvariable=self.find_text_var).pack(fill="x")
        locate_buttons = ttk.Frame(locate_box)
        locate_buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(locate_buttons, text="定位", command=self.locate_text_async).pack(side=LEFT, fill="x", expand=True)
        ttk.Button(locate_buttons, text="点击", command=self.click_text_async).pack(side=RIGHT, fill="x", expand=True, padx=(8, 0))
        ttk.Label(locate_box, textvariable=self.position_var).pack(anchor="w", pady=(8, 0))

        option_box = ttk.LabelFrame(tool_tab, text="选项快捷点击", padding=8)
        option_box.pack(fill="x", pady=(12, 0))
        ttk.Label(option_box, text="按 A/B/C/D 定位到左侧选项圆圈中心。").pack(anchor="w")
        option_locate = ttk.Frame(option_box)
        option_locate.pack(fill="x", pady=(8, 0))
        option_click = ttk.Frame(option_box)
        option_click.pack(fill="x", pady=(6, 0))
        for letter in ("A", "B", "C", "D"):
            ttk.Button(option_locate, text=f"定位 {letter}", command=lambda value=letter: self.locate_choice_async(value)).pack(
                side=LEFT,
                fill="x",
                expand=True,
                padx=(0, 6),
            )
            ttk.Button(option_click, text=f"点击 {letter}", command=lambda value=letter: self.click_choice_async(value)).pack(
                side=LEFT,
                fill="x",
                expand=True,
                padx=(0, 6),
            )

        result_box = ttk.LabelFrame(main_pane, text="运行结果", padding=10)
        main_pane.add(result_box, weight=4)
        self.result_box = result_box
        output_frame = ttk.Frame(result_box)
        output_frame.pack(fill=BOTH, expand=True)
        self.output = Text(
            output_frame,
            height=20,
            wrap="word",
            bg="#111827",
            fg="#e5e7eb",
            insertbackground="#e5e7eb",
            relief="flat",
            padx=10,
            pady=8,
            font=("Microsoft YaHei UI", 10),
            width=72,
        )
        output_scroll = ttk.Scrollbar(output_frame, orient="vertical", command=self.output.yview)
        self.output.configure(yscrollcommand=output_scroll.set)
        self.output.tag_configure("error", foreground="#fecaca")
        self.output.tag_configure("warning", foreground="#fde68a")
        self.output.pack(side=LEFT, fill=BOTH, expand=True)
        output_scroll.pack(side=RIGHT, fill="y")
        self.update_run_buttons()

    def build_browser_tab(self, parent) -> None:
        ttk.Checkbutton(parent, text="开始循环时启动浏览器批处理", variable=self.browser_enabled_var).pack(anchor="w")

        path_box = ttk.LabelFrame(parent, text="浏览器", padding=8)
        path_box.pack(fill="x", pady=(10, 0))
        ttk.Entry(path_box, textvariable=self.browser_path_var).pack(fill="x")
        path_buttons = ttk.Frame(path_box)
        path_buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(path_buttons, text="自动检测", command=self.detect_browser_path_to_ui).pack(side=LEFT, fill="x", expand=True)
        ttk.Button(path_buttons, text="选择 exe", command=self.choose_browser_path).pack(side=RIGHT, fill="x", expand=True, padx=(8, 0))

        window_box = ttk.LabelFrame(parent, text="窗口位置和大小", padding=8)
        window_box.pack(fill="x", pady=(10, 0))
        for col, key in enumerate(("x", "y", "w", "h")):
            ttk.Label(window_box, text=key.upper()).grid(row=0, column=col, padx=4, sticky="w")
            ttk.Entry(window_box, textvariable=self.browser_window_vars[key], width=8).grid(row=1, column=col, padx=4, sticky="ew")
            window_box.columnconfigure(col, weight=1)

        misc_box = ttk.LabelFrame(parent, text="批处理参数", padding=8)
        misc_box.pack(fill="x", pady=(10, 0))
        misc_box.columnconfigure(1, weight=1)
        ttk.Label(misc_box, text="DevTools 端口").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(misc_box, textvariable=self.browser_debug_port_var, width=8).grid(row=0, column=1, sticky="ew", padx=(8, 0), pady=4)
        ttk.Label(misc_box, text="页面加载等待秒数").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(misc_box, textvariable=self.browser_wait_seconds_var, width=8).grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=4)
        ttk.Label(misc_box, text="完成后跳转等待秒数").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(misc_box, textvariable=self.browser_next_wait_seconds_var, width=8).grid(row=2, column=1, sticky="ew", padx=(8, 0), pady=4)

        urls_box = ttk.LabelFrame(parent, text="网址列表，每行一个", padding=8)
        urls_box.pack(fill=BOTH, expand=True, pady=(10, 0))
        self.browser_urls_input = Text(
            urls_box,
            height=9,
            width=28,
            wrap="none",
            bg="#ffffff",
            fg="#0f172a",
            insertbackground="#2563eb",
            relief="flat",
            padx=8,
            pady=6,
            font=("Microsoft YaHei UI", 10),
        )
        self.browser_urls_input.insert("1.0", "\n".join(self.config.browser_urls))
        self.browser_urls_input.pack(fill=BOTH, expand=True)
        ttk.Label(parent, textvariable=self.batch_status_var, style="Muted.TLabel", wraplength=360, justify="left").pack(fill="x", pady=(8, 0))

    def detect_browser_path_to_ui(self) -> None:
        path = detect_browser_path()
        if path:
            self.browser_path_var.set(path)
            self.status_var.set("已自动检测到浏览器")
        else:
            self.report_error("未找到浏览器", "没有自动找到 Edge/Chrome，请手动选择浏览器 exe。")

    def choose_browser_path(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 Edge/Chrome 浏览器",
            filetypes=[("浏览器", "msedge.exe chrome.exe"), ("可执行文件", "*.exe"), ("所有文件", "*.*")],
        )
        if path:
            self.browser_path_var.set(path)

    def build_debug_tab(self, parent) -> None:
        top = ttk.Frame(parent)
        top.pack(fill="x")
        ttk.Button(top, text="刷新相似度调试", command=self.refresh_similarity_debug_view).pack(side=LEFT)
        ttk.Button(top, text="清空相似度历史", command=self.clear_similarity_history).pack(side=LEFT, padx=(8, 0))

        info_box = ttk.LabelFrame(parent, text="最近一次截图相似判断", padding=8)
        info_box.pack(fill="x", pady=(10, 0))
        self.debug_info = Text(
            info_box,
            height=8,
            wrap="word",
            bg="#0f172a",
            fg="#e5e7eb",
            insertbackground="#e5e7eb",
            relief="flat",
            padx=8,
            pady=6,
            font=("Consolas", 9),
        )
        self.debug_info.pack(fill="x")

        pair_box = ttk.LabelFrame(parent, text="本次参与对比的截图", padding=8)
        pair_box.pack(fill="x", pady=(10, 0))
        pair_box.columnconfigure(0, weight=1)
        pair_box.columnconfigure(1, weight=1)
        pair_box.columnconfigure(2, weight=1)
        self.debug_previous_label = ttk.Label(pair_box, text="历史图：暂无", anchor="center", justify="center", style="Muted.TLabel")
        self.debug_previous_label.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.debug_current_label = ttk.Label(pair_box, text="当前图：暂无", anchor="center", justify="center", style="Muted.TLabel")
        self.debug_current_label.grid(row=0, column=1, sticky="nsew", padx=6)
        self.debug_diff_label = ttk.Label(pair_box, text="差异图：暂无", anchor="center", justify="center", style="Muted.TLabel")
        self.debug_diff_label.grid(row=0, column=2, sticky="nsew", padx=(6, 0))

        history_box = ttk.LabelFrame(parent, text="相似度历史截图，右侧为最新", padding=8)
        history_box.pack(fill=BOTH, expand=True, pady=(10, 0))
        self.debug_history_labels = []
        for column in range(4):
            history_box.columnconfigure(column, weight=1)
            label = ttk.Label(history_box, text=f"历史 {column + 1}\n暂无", anchor="center", justify="center", style="Muted.TLabel")
            label.grid(row=0, column=column, sticky="nsew", padx=4)
            self.debug_history_labels.append(label)
        self.refresh_similarity_debug_view()

    def clear_similarity_history(self) -> None:
        self.worker.reset_similarity_reference()
        self.refresh_similarity_debug_view()
        self.append_output(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 已清空相似度历史\n{'-' * 48}")

    def refresh_similarity_debug_view(self) -> None:
        if not hasattr(self, "debug_info"):
            return
        snapshot = self.worker.similarity_debug_snapshot()
        self.update_similarity_debug_view(snapshot)

    def debug_thumbnail(self, entry: dict | None, title: str, max_size: tuple[int, int] = (300, 210)) -> ImageTk.PhotoImage | None:
        if not entry or entry.get("image") is None:
            return None
        image = entry["image"].convert("RGB").copy()
        image.thumbnail(max_size)
        canvas = Image.new("RGB", (max_size[0], max_size[1] + 38), "#f8fafc")
        x = (max_size[0] - image.width) // 2
        y = 28 + (max_size[1] - image.height) // 2
        canvas.paste(image, (x, y))
        draw = ImageDraw.Draw(canvas)
        draw.rectangle((x, y, x + image.width - 1, y + image.height - 1), outline="#2563eb", width=2)
        draw.text((8, 8), title, fill="#0f172a")
        return ImageTk.PhotoImage(canvas)

    def debug_diff_thumbnail(self, previous: dict | None, current: dict | None) -> ImageTk.PhotoImage | None:
        if not previous or not current or previous.get("image") is None or current.get("image") is None:
            return None
        prev = previous["image"].convert("RGB")
        curr = current["image"].convert("RGB")
        if prev.size != curr.size:
            prev = prev.resize(curr.size)
        diff = ImageChops.difference(prev, curr).point(lambda value: min(255, value * 4))
        return self.debug_thumbnail({"image": diff, "seq": "-", "time": "", "size": diff.size}, "差异图 x4")

    def update_similarity_debug_view(self, snapshot: dict) -> None:
        comparison = snapshot.get("comparison") or {}
        current = snapshot.get("current")
        previous = snapshot.get("previous")
        history = snapshot.get("history") or []
        lines = []
        if current:
            lines.append(f"current_seq =>\t{current.get('seq')}  time =>\t{current.get('time')}  size =>\t{current.get('size')}")
        else:
            lines.append("current_seq =>\t暂无")
        if previous:
            lines.append(f"previous_seq =>\t{previous.get('seq')}  time =>\t{previous.get('time')}  size =>\t{previous.get('size')}")
        else:
            lines.append("previous_seq =>\t暂无")
        if comparison:
            similarity = comparison.get("similarity")
            similarity_text = f"{similarity:.2f}%" if isinstance(similarity, (int, float)) else "-"
            legacy_similarity = comparison.get("legacy_similarity")
            legacy_similarity_text = f"{legacy_similarity:.2f}%" if isinstance(legacy_similarity, (int, float)) else "-"
            mean_diff = comparison.get("mean_diff")
            mean_diff_text = f"{mean_diff:.3f}" if isinstance(mean_diff, (int, float)) else "-"
            legacy_mean_diff = comparison.get("legacy_mean_diff")
            legacy_mean_diff_text = f"{legacy_mean_diff:.3f}" if isinstance(legacy_mean_diff, (int, float)) else "-"
            changed_ratio = comparison.get("changed_ratio")
            changed_ratio_text = f"{changed_ratio * 100:.2f}%" if isinstance(changed_ratio, (int, float)) else "-"
            lines.extend(
                [
                    f"lag =>\t{comparison.get('lag')}  threshold =>\t{comparison.get('threshold')}%  hit =>\t{comparison.get('hit')}",
                    f"foreground_similarity =>\t{similarity_text}  foreground_mean_diff =>\t{mean_diff_text}",
                    f"legacy_full_similarity =>\t{legacy_similarity_text}  legacy_mean_diff =>\t{legacy_mean_diff_text}",
                    f"foreground_pixels =>\t{comparison.get('foreground_pixels')}  changed_pixels =>\t{comparison.get('changed_pixels')}  changed_ratio =>\t{changed_ratio_text}",
                    f"foreground_threshold =>\t{comparison.get('foreground_threshold')}  difference_threshold =>\t{comparison.get('difference_threshold')}",
                    f"history_count_before =>\t{comparison.get('history_count_before')}  history_count_after =>\t{comparison.get('history_count_after')}",
                ]
            )
        else:
            lines.append("comparison =>\t暂无可用对比，可能历史截图不足")
        lines.append("提示 =>\t现在命中使用 foreground_similarity；legacy_full_similarity 只用于排查白底导致的旧算法虚高。")
        self.debug_info.delete("1.0", END)
        self.debug_info.insert("1.0", "\n".join(lines))

        self.debug_photos = []
        previous_photo = self.debug_thumbnail(previous, "历史对比图")
        if previous_photo:
            self.debug_photos.append(previous_photo)
            self.debug_previous_label.configure(image=previous_photo, text="")
        else:
            self.debug_previous_label.configure(image="", text="历史图：暂无")
        current_photo = self.debug_thumbnail(current, "当前截图")
        if current_photo:
            self.debug_photos.append(current_photo)
            self.debug_current_label.configure(image=current_photo, text="")
        else:
            self.debug_current_label.configure(image="", text="当前图：暂无")
        diff_photo = self.debug_diff_thumbnail(previous, current)
        if diff_photo:
            self.debug_photos.append(diff_photo)
            self.debug_diff_label.configure(image=diff_photo, text="")
        else:
            self.debug_diff_label.configure(image="", text="差异图：暂无")

        recent = history[-4:]
        for label in self.debug_history_labels:
            label.configure(image="", text="暂无")
        for index, entry in enumerate(recent):
            title = f"#{entry.get('seq')} {entry.get('time')} {'最新' if index == len(recent) - 1 else ''}".strip()
            photo = self.debug_thumbnail(entry, title, max_size=(190, 150))
            if photo:
                self.debug_photos.append(photo)
                self.debug_history_labels[index].configure(image=photo, text="")

    def build_workflow_tab(self, parent) -> None:
        pane = ttk.PanedWindow(parent, orient="horizontal")
        pane.pack(fill=BOTH, expand=True)
        left_panel = ttk.Frame(pane)
        right_panel = ttk.Frame(pane)
        pane.add(left_panel, weight=2)
        pane.add(right_panel, weight=3)

        actions_box = ttk.LabelFrame(left_panel, text="循环动作", padding=8)
        actions_box.pack(fill=BOTH, expand=True)

        columns = ("enabled", "action", "detail")
        self.workflow_tree = ttk.Treeview(actions_box, columns=columns, show="headings", height=9, selectmode="browse")
        self.workflow_tree.heading("enabled", text="启用")
        self.workflow_tree.heading("action", text="动作")
        self.workflow_tree.heading("detail", text="参数")
        self.workflow_tree.column("enabled", width=54, anchor="center", stretch=False)
        self.workflow_tree.column("action", width=98, anchor="w", stretch=False)
        self.workflow_tree.column("detail", width=220, anchor="w", stretch=True)
        tree_scroll = ttk.Scrollbar(actions_box, orient="vertical", command=self.workflow_tree.yview)
        self.workflow_tree.configure(yscrollcommand=tree_scroll.set)
        self.workflow_tree.grid(row=0, column=0, sticky="nsew")
        tree_scroll.grid(row=0, column=1, sticky="ns")
        self.workflow_tree.tag_configure("running", background="#dbeafe", foreground="#1e3a8a")
        self.workflow_tree.tag_configure("done", background="#dcfce7", foreground="#166534")
        self.workflow_tree.tag_configure("skipped", background="#f1f5f9", foreground="#64748b")
        self.workflow_tree.tag_configure("error", background="#fee2e2", foreground="#991b1b")
        actions_box.columnconfigure(0, weight=1)
        actions_box.rowconfigure(0, weight=1)
        self.workflow_tree.bind("<<TreeviewSelect>>", self.on_workflow_select)

        button_bar = ttk.Frame(left_panel)
        button_bar.pack(fill="x", pady=(8, 0))
        ttk.Button(button_bar, text="新增", width=5, command=self.add_workflow_action).pack(side=LEFT)
        ttk.Button(button_bar, text="删除", width=5, command=self.delete_workflow_action).pack(side=LEFT, padx=(6, 0))
        ttk.Button(button_bar, text="上移", width=5, command=lambda: self.move_workflow_action(-1)).pack(side=LEFT, padx=(6, 0))
        ttk.Button(button_bar, text="下移", width=5, command=lambda: self.move_workflow_action(1)).pack(side=LEFT, padx=(6, 0))
        ttk.Button(button_bar, text="默认", width=5, command=self.reset_workflow_actions).pack(side=RIGHT)

        editor = ttk.LabelFrame(right_panel, text="动作参数", padding=12)
        editor.pack(fill=BOTH, expand=True)
        editor.columnconfigure(0, weight=1)

        ttk.Label(editor, textvariable=self.action_title_var, style="Surface.TLabel", font=("Microsoft YaHei UI", 10, "bold")).grid(row=0, column=0, sticky="ew")
        ttk.Label(editor, textvariable=self.action_hint_var, style="Muted.TLabel", wraplength=520, justify="left").grid(row=1, column=0, sticky="ew", pady=(4, 10))

        common = ttk.Frame(editor, style="Surface.TFrame")
        common.grid(row=2, column=0, sticky="ew")
        common.columnconfigure(1, weight=1)
        ttk.Label(common, text="动作类型", style="Surface.TLabel").grid(row=0, column=0, sticky="w", pady=4)
        self.action_type_combo = ttk.Combobox(common, textvariable=self.action_type_var, values=ACTION_TYPE_OPTIONS, state="readonly", width=18)
        self.action_type_combo.grid(row=0, column=1, sticky="ew", padx=(8, 0), pady=4)
        self.action_type_combo.bind("<<ComboboxSelected>>", self.on_action_type_changed)
        ttk.Checkbutton(common, text="启用这个动作", variable=self.action_enabled_var).grid(row=1, column=1, sticky="w", padx=(8, 0), pady=4)

        self.action_param_host = ttk.Frame(editor, style="Surface.TFrame")
        self.action_param_host.grid(row=3, column=0, sticky="nsew", pady=(10, 0))
        self.action_param_host.columnconfigure(0, weight=1)
        self.action_param_host.rowconfigure(0, weight=1)
        editor.rowconfigure(3, weight=1)
        self.action_param_frames: dict[str, ttk.Frame] = {}
        self.build_action_param_frames()

        footer = ttk.Frame(editor, style="Surface.TFrame")
        footer.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        footer.columnconfigure(0, weight=1)
        footer.columnconfigure(1, weight=1)
        ttk.Button(footer, text="保存当前动作", command=self.save_action_editor).grid(row=0, column=0, sticky="ew")
        ttk.Button(footer, text="立即试运行流程", command=self.run_once_async, style="Accent.TButton").grid(row=0, column=1, sticky="ew", padx=(8, 0))

        self.refresh_action_tree(select_index=0)

    def add_param_row(self, parent, row: int, label: str, widget) -> None:
        parent.columnconfigure(1, weight=1)
        ttk.Label(parent, text=label, style="Surface.TLabel").grid(row=row, column=0, sticky="w", pady=5)
        widget.grid(row=row, column=1, sticky="ew", padx=(10, 0), pady=5)

    def build_action_param_frames(self) -> None:
        self.action_param_frames = {}

        frame = ttk.Frame(self.action_param_host, style="Surface.TFrame")
        ttk.Label(frame, text="每次执行到这里都会重新截图并运行 OCR，结果会写入缓存。", style="Muted.TLabel", wraplength=420, justify="left").grid(row=0, column=0, sticky="ew")
        self.action_param_frames["ocr"] = frame

        frame = ttk.Frame(self.action_param_host, style="Surface.TFrame")
        self.add_param_row(frame, 0, "判断类型", ttk.Combobox(frame, textvariable=self.action_condition_var, values=list(CONDITION_OPTIONS), state="readonly"))
        self.add_param_row(frame, 1, "检查来源", ttk.Combobox(frame, textvariable=self.action_source_var, values=list(SOURCE_OPTIONS), state="readonly"))
        self.add_param_row(frame, 2, "匹配方式", ttk.Combobox(frame, textvariable=self.action_mode_var, values=list(MODE_OPTIONS), state="readonly"))
        self.add_param_row(frame, 3, "条件文字", ttk.Entry(frame, textvariable=self.action_text_var))
        self.add_param_row(frame, 4, "相似阈值 %", ttk.Entry(frame, textvariable=self.action_similarity_threshold_var, width=8))
        self.add_param_row(frame, 5, "回看截图数", ttk.Combobox(frame, textvariable=self.action_similarity_lag_var, values=["1", "2", "3", "4"], state="readonly", width=8))
        true_row = ttk.Frame(frame, style="Surface.TFrame")
        true_row.columnconfigure(0, weight=1)
        ttk.Combobox(true_row, textvariable=self.action_true_branch_var, values=list(BRANCH_OPTIONS), state="readonly", width=14).grid(row=0, column=0, sticky="ew")
        ttk.Entry(true_row, textvariable=self.action_true_value_var, width=8).grid(row=0, column=1, padx=(8, 0))
        self.add_param_row(frame, 6, "满足时", true_row)
        false_row = ttk.Frame(frame, style="Surface.TFrame")
        false_row.columnconfigure(0, weight=1)
        ttk.Combobox(false_row, textvariable=self.action_false_branch_var, values=list(BRANCH_OPTIONS), state="readonly", width=14).grid(row=0, column=0, sticky="ew")
        ttk.Entry(false_row, textvariable=self.action_false_value_var, width=8).grid(row=0, column=1, padx=(8, 0))
        self.add_param_row(frame, 7, "不满足时", false_row)
        ttk.Label(frame, text="截图相似会比较当前目标区域和 N 张之前的相似度判断截图；N 可选 1-4，默认 1。命中分数按文字/按钮等前景像素计算，调试页会同时显示旧整图分数。", style="Muted.TLabel", wraplength=420, justify="left").grid(row=8, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Label(frame, text="数值含义：跳过=跳过后面 N 步；跳到=跳到第 N 步。继续/停止时可忽略右侧数值。", style="Muted.TLabel", wraplength=420, justify="left").grid(row=9, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self.action_param_frames["if_text"] = frame

        frame = ttk.Frame(self.action_param_host, style="Surface.TFrame")
        self.add_param_row(frame, 0, "点击文字", ttk.Entry(frame, textvariable=self.action_text_var))
        self.add_param_row(frame, 1, "点击区域", ttk.Combobox(frame, textvariable=self.action_click_area_var, values=list(CLICK_AREA_OPTIONS), state="readonly"))
        ttk.Label(frame, text="选择题里输入 A/B/C/D 或选项内容时，通常选择“选项圆圈”。", style="Muted.TLabel", wraplength=420, justify="left").grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.action_param_frames["click_text"] = frame

        frame = ttk.Frame(self.action_param_host, style="Surface.TFrame")
        self.add_param_row(frame, 0, "等待秒数", ttk.Entry(frame, textvariable=self.action_seconds_var, width=8))
        self.action_param_frames["wait"] = frame

        frame = ttk.Frame(self.action_param_host, style="Surface.TFrame")
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text="AI 提示语", style="Surface.TLabel").grid(row=0, column=0, sticky="w")
        self.action_prompt_input = Text(
            frame,
            height=7,
            width=24,
            wrap="word",
            bg="#ffffff",
            fg="#0f172a",
            insertbackground="#2563eb",
            relief="flat",
            padx=8,
            pady=6,
            font=("Microsoft YaHei UI", 10),
        )
        self.action_prompt_input.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        frame.rowconfigure(1, weight=1)
        ttk.Label(frame, text="可用占位符：{U} 提示语、{Q} 格式化题目、{OCR} 原始OCR、{AI} 上次AI回答、{MATCHES} 解析结果。", style="Muted.TLabel", wraplength=420, justify="left").grid(row=2, column=0, sticky="ew", pady=(6, 0))
        image_row = ttk.Frame(frame, style="Surface.TFrame")
        image_row.columnconfigure(0, weight=1)
        ttk.Checkbutton(image_row, text="发送当前截图给 AI", variable=self.action_include_image_var).grid(row=0, column=0, sticky="w")
        ttk.Label(image_row, text="默认关闭；开启后会把目标区域 PNG 与文本一起发送。", style="Muted.TLabel").grid(row=1, column=0, sticky="ew", pady=(2, 0))
        image_row.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self.action_param_frames["ask_ai"] = frame

        frame = ttk.Frame(self.action_param_host, style="Surface.TFrame")
        self.add_param_row(frame, 0, "解析来源", ttk.Combobox(frame, textvariable=self.action_source_var, values=list(SOURCE_OPTIONS), state="readonly"))
        self.add_param_row(frame, 1, "正则表达式", ttk.Entry(frame, textvariable=self.action_regex_var))
        ttk.Label(frame, text=r"示例：正确答案[:：]\s*([^\n\r]+)，可解析 A,C、选项文字、多选文字，也可解析判断题的正确/错误。", style="Muted.TLabel", wraplength=420, justify="left").grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.action_param_frames["parse_ai"] = frame

        frame = ttk.Frame(self.action_param_host, style="Surface.TFrame")
        self.add_param_row(frame, 0, "点击区域", ttk.Combobox(frame, textvariable=self.action_click_area_var, values=list(CLICK_AREA_OPTIONS), state="readonly"))
        self.add_param_row(frame, 1, "点击间隔", ttk.Entry(frame, textvariable=self.action_delay_var, width=8))
        ttk.Label(frame, text="会点击“解析 AI”动作得到的文字答案、A/B/C/D，或判断题的正确/错误。多选会按顺序点击。", style="Muted.TLabel", wraplength=420, justify="left").grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.action_param_frames["click_matches"] = frame

    def show_action_param_frame(self, action_type: str) -> None:
        if not hasattr(self, "action_param_frames"):
            return
        for frame in self.action_param_frames.values():
            frame.grid_forget()
        frame = self.action_param_frames.get(action_type) or self.action_param_frames.get("ocr")
        if frame:
            frame.grid(row=0, column=0, sticky="nsew")
        self.action_hint_var.set(ACTION_HINTS.get(action_type, ""))

    def workflow_action_detail(self, action: dict) -> str:
        params = action.get("params") or {}
        action_type = action.get("type")
        if action_type == "ocr":
            return "强制刷新截图" if params.get("force", True) else "复用缓存"
        if action_type == "if_text":
            condition = params.get("condition", "text")
            if condition == "screenshot_similarity":
                true_text = self.branch_summary(params.get("true_action", "continue"), params.get("true_value", "1"))
                false_text = self.branch_summary(params.get("false_action", "skip"), params.get("false_value", "1"))
                return f"截图相似 >= {params.get('similarity_threshold', 90)}% / 回看 {params.get('similarity_lag', 1)} 张 / 是:{true_text} / 否:{false_text}"
            source = SOURCE_LABELS.get(params.get("source", "ocr"), "OCR 文本")
            mode = MODE_LABELS.get(params.get("mode", "all"), "全部包含")
            true_text = self.branch_summary(params.get("true_action", "continue"), params.get("true_value", "1"))
            false_text = self.branch_summary(params.get("false_action", "skip"), params.get("false_value", params.get("skip_next", "1")))
            return f"{source} / {mode} / {params.get('text', '')} / 是:{true_text} / 否:{false_text}"
        if action_type == "click_text":
            return f"{params.get('text', '')} / {CLICK_AREA_LABELS.get(params.get('click_area', 'letter'), '选项圆圈')}"
        if action_type == "wait":
            return f"{params.get('seconds', 1)} 秒"
        if action_type == "ask_ai":
            prompt = " ".join((params.get("prompt") or "{U} Q:{Q}").split())
            prefix = "带图 / " if bool_value(params.get("include_image", False), False) else "纯文本 / "
            return prefix + prompt[:72]
        if action_type == "parse_ai":
            return params.get("regex", "")
        if action_type == "click_matches":
            return f"{CLICK_AREA_LABELS.get(params.get('click_area', 'letter'), '选项圆圈')} / 间隔 {params.get('delay', 0.2)} 秒"
        return ""

    def branch_summary(self, branch_action: str, branch_value) -> str:
        label = BRANCH_LABELS.get(branch_action, "继续下一步")
        if branch_action in {"skip", "jump"}:
            return f"{label} {branch_value}"
        return label

    def refresh_action_tree(self, select_index: int | None = None) -> None:
        if not hasattr(self, "workflow_tree"):
            return
        self._loading_action_editor = True
        self.workflow_tree.delete(*self.workflow_tree.get_children())
        for index, action in enumerate(self.workflow_actions):
            self.workflow_tree.insert(
                "",
                END,
                iid=str(index),
                values=(
                    "是" if action.get("enabled", True) else "否",
                    ACTION_TYPES.get(action.get("type"), action.get("type", "")),
                    self.workflow_action_detail(action),
                ),
            )
        if self.workflow_actions:
            index = 0 if select_index is None else max(0, min(select_index, len(self.workflow_actions) - 1))
            self.workflow_tree.selection_set(str(index))
            self.workflow_tree.see(str(index))
            self._loading_action_editor = False
            self.load_action_editor(index)
        else:
            self._loading_action_editor = False

    def update_workflow_tree_row(self, index: int) -> None:
        if not hasattr(self, "workflow_tree") or index < 0 or index >= len(self.workflow_actions):
            return
        iid = str(index)
        if not self.workflow_tree.exists(iid):
            return
        action = self.workflow_actions[index]
        self.workflow_tree.item(
            iid,
            values=(
                "是" if action.get("enabled", True) else "否",
                ACTION_TYPES.get(action.get("type"), action.get("type", "")),
                self.workflow_action_detail(action),
            ),
        )

    def clear_workflow_step_marks(self) -> None:
        if not hasattr(self, "workflow_tree"):
            return
        for iid in self.workflow_tree.get_children():
            self.workflow_tree.item(iid, tags=())

    def mark_workflow_step(self, index: int, state: str) -> None:
        if not hasattr(self, "workflow_tree"):
            return
        iid = str(index)
        if not self.workflow_tree.exists(iid):
            return
        tag = {
            "running": "running",
            "done": "done",
            "skip": "skipped",
            "error": "error",
        }.get(state, "")
        self.workflow_tree.item(iid, tags=(tag,) if tag else ())
        self.workflow_tree.see(iid)

    def selected_action_index(self) -> int | None:
        if not hasattr(self, "workflow_tree"):
            return None
        selection = self.workflow_tree.selection()
        if not selection:
            return None
        try:
            return int(selection[0])
        except ValueError:
            return None

    def on_workflow_select(self, _event=None) -> None:
        if self._loading_action_editor:
            return
        index = self.selected_action_index()
        if index is None:
            return
        if self.current_action_index == index:
            return
        if self.current_action_index is not None and self.current_action_index != index:
            self.save_action_editor_to_index(self.current_action_index, silent=True, refresh=False)
            self.update_workflow_tree_row(self.current_action_index)
        self.load_action_editor(index)

    def load_action_editor(self, index: int) -> None:
        if index < 0 or index >= len(self.workflow_actions):
            return
        self._loading_action_editor = True
        action = self.workflow_actions[index]
        action_type = action.get("type", "ocr")
        params = default_action_params(action_type)
        params.update(action.get("params") or {})
        self.current_action_index = index
        self.action_title_var.set(f"正在编辑第 {index + 1} 个动作：{ACTION_TYPES.get(action_type, action_type)}")
        self.action_type_var.set(f"{ACTION_TYPES.get(action_type, action_type)} ({action_type})")
        self.action_enabled_var.set(bool(action.get("enabled", True)))
        self.apply_action_params(action_type, params)
        self.show_action_param_frame(action_type)
        self._loading_action_editor = False

    def apply_action_params(self, action_type: str, params: dict) -> None:
        self.action_condition_var.set(CONDITION_LABELS.get(params.get("condition", "text"), "文本包含"))
        self.action_text_var.set(params.get("text", ""))
        self.action_seconds_var.set(str(params.get("seconds", "1")))
        self.action_skip_var.set(str(params.get("skip_next", "1")))
        self.action_regex_var.set(params.get("regex", DEFAULT_PARSE_REGEX))
        source_default = "ai" if action_type == "parse_ai" else "ocr"
        self.action_source_var.set(SOURCE_LABELS.get(params.get("source", source_default), "OCR 文本"))
        self.action_mode_var.set(MODE_LABELS.get(params.get("mode", "all"), "全部包含"))
        self.action_similarity_threshold_var.set(str(params.get("similarity_threshold", "90")))
        self.action_similarity_lag_var.set(str(self.worker.similarity_lag(params.get("similarity_lag", "1"))))
        self.action_click_area_var.set(CLICK_AREA_LABELS.get(params.get("click_area", "letter"), "选项圆圈"))
        self.action_delay_var.set(str(params.get("delay", "0.2")))
        self.action_include_image_var.set(bool_value(params.get("include_image", False), False))
        self.action_true_branch_var.set(BRANCH_LABELS.get(params.get("true_action", "continue"), "继续下一步"))
        self.action_true_value_var.set(str(params.get("true_value", "1")))
        false_action = params.get("false_action", "skip")
        false_value = params.get("false_value", params.get("skip_next", "1"))
        self.action_false_branch_var.set(BRANCH_LABELS.get(false_action, "跳过后面 N 步"))
        self.action_false_value_var.set(str(false_value))
        if hasattr(self, "action_prompt_input"):
            self.action_prompt_input.delete("1.0", END)
            self.action_prompt_input.insert("1.0", params.get("prompt", "{U}\n\nQ:\n{Q}"))

    def on_action_type_changed(self, _event=None) -> None:
        if self._loading_action_editor:
            return
        action_type = ACTION_OPTION_TO_KEY.get(self.action_type_var.get(), "ocr")
        self.apply_action_params(action_type, default_action_params(action_type))
        self.show_action_param_frame(action_type)
        if self.current_action_index is not None:
            self.action_title_var.set(f"正在编辑第 {self.current_action_index + 1} 个动作：{ACTION_TYPES.get(action_type, action_type)}")
        self.status_var.set(f"已切换为“{ACTION_TYPES.get(action_type, action_type)}”，请填写该动作参数")

    def action_from_editor(self) -> dict:
        action_type = ACTION_OPTION_TO_KEY.get(self.action_type_var.get(), "ocr")
        params = {
            "condition": CONDITION_OPTIONS.get(self.action_condition_var.get(), "text"),
            "text": self.action_text_var.get().strip(),
            "seconds": self.action_seconds_var.get().strip() or "1",
            "skip_next": self.action_skip_var.get().strip() or "1",
            "regex": self.action_regex_var.get().strip(),
            "source": SOURCE_OPTIONS.get(self.action_source_var.get(), "ocr"),
            "mode": MODE_OPTIONS.get(self.action_mode_var.get(), "all"),
            "similarity_threshold": self.action_similarity_threshold_var.get().strip() or "90",
            "similarity_lag": self.action_similarity_lag_var.get().strip() or "1",
            "click_area": CLICK_AREA_OPTIONS.get(self.action_click_area_var.get(), "letter"),
            "delay": self.action_delay_var.get().strip() or "0.2",
            "include_image": bool(self.action_include_image_var.get()),
            "prompt": self.action_prompt_input.get("1.0", END).strip(),
            "true_action": BRANCH_OPTIONS.get(self.action_true_branch_var.get(), "continue"),
            "true_value": self.action_true_value_var.get().strip() or "1",
            "false_action": BRANCH_OPTIONS.get(self.action_false_branch_var.get(), "skip"),
            "false_value": self.action_false_value_var.get().strip() or "1",
        }
        if action_type == "ocr":
            params = {"force": True}
        elif action_type == "wait":
            params = {"seconds": params["seconds"]}
        elif action_type == "ask_ai":
            params = {"prompt": params["prompt"] or "{U}\n\nQ:\n{Q}", "include_image": params["include_image"]}
        elif action_type == "parse_ai":
            params = {"regex": params["regex"], "source": params["source"]}
        elif action_type == "click_matches":
            params = {"click_area": params["click_area"], "delay": params["delay"]}
        elif action_type == "click_text":
            params = {"text": params["text"], "click_area": params["click_area"]}
        elif action_type == "if_text":
            params = {
                "condition": params["condition"],
                "text": params["text"],
                "source": params["source"],
                "mode": params["mode"],
                "similarity_threshold": params["similarity_threshold"],
                "similarity_lag": params["similarity_lag"],
                "true_action": params["true_action"],
                "true_value": params["true_value"],
                "false_action": params["false_action"],
                "false_value": params["false_value"],
            }
        return {"enabled": bool(self.action_enabled_var.get()), "type": action_type, "params": params}

    def save_action_editor_to_index(self, index: int | None, silent: bool = False, refresh: bool = True) -> None:
        if index is None:
            return
        self.workflow_actions[index] = self.action_from_editor()
        if refresh:
            self.refresh_action_tree(select_index=index)
        if not silent:
            self.status_var.set(f"第 {index + 1} 个动作已保存：{ACTION_TYPES.get(self.workflow_actions[index]['type'], self.workflow_actions[index]['type'])}")

    def save_action_editor(self, silent: bool = False) -> None:
        index = self.current_action_index
        if index is None:
            index = self.selected_action_index()
        self.save_action_editor_to_index(index, silent=silent, refresh=True)

    def add_workflow_action(self) -> None:
        self.save_action_editor(silent=True)
        self.workflow_actions.append({"enabled": True, "type": "wait", "params": {"seconds": "1"}})
        self.refresh_action_tree(select_index=len(self.workflow_actions) - 1)

    def delete_workflow_action(self) -> None:
        index = self.selected_action_index()
        if index is None:
            return
        del self.workflow_actions[index]
        if not self.workflow_actions:
            self.workflow_actions = default_workflow_actions()
        self.refresh_action_tree(select_index=min(index, len(self.workflow_actions) - 1))

    def move_workflow_action(self, delta: int) -> None:
        index = self.selected_action_index()
        if index is None:
            return
        self.save_action_editor(silent=True)
        new_index = index + delta
        if new_index < 0 or new_index >= len(self.workflow_actions):
            return
        self.workflow_actions[index], self.workflow_actions[new_index] = self.workflow_actions[new_index], self.workflow_actions[index]
        self.refresh_action_tree(select_index=new_index)

    def reset_workflow_actions(self) -> None:
        self.workflow_actions = default_workflow_actions()
        self.refresh_action_tree(select_index=0)
        self.status_var.set("流程已恢复默认")

    def add_entry(self, parent, label: str, variable: StringVar, show: str | None = None) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text=label).pack(anchor="w")
        ttk.Entry(row, textvariable=variable, show=show).pack(fill="x")

    def on_api_provider_changed(self, _event=None) -> None:
        provider = API_PROVIDER_OPTIONS.get(self.api_provider_var.get(), "gemini")
        base_url = self.base_url_var.get().strip().rstrip("/")
        model = self.model_var.get().strip()
        if provider == "openai":
            if not base_url or base_url.endswith("/v1beta"):
                self.base_url_var.set(DEFAULT_OPENAI_BASE_URL)
            if not model or model.startswith("gemini-"):
                self.model_var.set(DEFAULT_OPENAI_MODEL)
            self.status_var.set("已切换到 OpenAI 兼容接口，默认模型 gpt-5.4")
        else:
            if not base_url or base_url.endswith("/v1"):
                self.base_url_var.set(DEFAULT_GEMINI_BASE_URL)
            if not model or model.startswith("gpt-"):
                self.model_var.set(DEFAULT_GEMINI_MODEL)
            self.status_var.set("已切换到 Gemini / Sub2API v1beta 接口")

    def read_region_from_ui(self) -> Region:
        return Region("target", int(self.region_vars["x"].get()), int(self.region_vars["y"].get()), int(self.region_vars["w"].get()), int(self.region_vars["h"].get()))

    def update_region_ui(self, region: Region) -> None:
        for key, value in {"x": region.x, "y": region.y, "w": region.w, "h": region.h}.items():
            self.region_vars[key].set(str(value))

    def save_from_ui(self) -> bool:
        try:
            if hasattr(self, "workflow_tree"):
                self.save_action_editor(silent=True)
            old_lang = self.config.paddle_lang
            old_cache_key = self.worker.cache_key()
            self.config.interval_seconds = max(1, int(self.interval_var.get()))
            self.config.region = self.read_region_from_ui()
            self.config.prompt_u = self.prompt_input.get("1.0", END).strip()
            self.config.api_provider = API_PROVIDER_OPTIONS.get(self.api_provider_var.get(), "gemini")
            default_base_url = DEFAULT_OPENAI_BASE_URL if self.config.api_provider == "openai" else DEFAULT_GEMINI_BASE_URL
            default_model = DEFAULT_OPENAI_MODEL if self.config.api_provider == "openai" else DEFAULT_GEMINI_MODEL
            self.config.api_base_url = self.base_url_var.get().strip() or default_base_url
            self.config.gemini_model = self.model_var.get().strip() or default_model
            self.config.api_key_env = "OPENAI_API_KEY" if self.config.api_provider == "openai" else "SUB2API_API_KEY"
            self.config.api_key = self.api_key_var.get().strip()
            self.config.paddle_lang = self.lang_var.get().strip() or "ch"
            self.config.use_choice_formatter = bool(self.choice_formatter_var.get())
            self.config.save_snapshots = bool(self.save_snapshots_var.get())
            self.config.answer_validation_pause_on_mismatch = bool(self.answer_validation_pause_var.get())
            self.config.browser_enabled = bool(self.browser_enabled_var.get())
            self.config.browser_path = self.browser_path_var.get().strip()
            self.config.browser_debug_port = max(1024, int(float(self.browser_debug_port_var.get() or 9223)))
            self.config.browser_window_x = int(float(self.browser_window_vars["x"].get() or 0))
            self.config.browser_window_y = int(float(self.browser_window_vars["y"].get() or 0))
            self.config.browser_window_w = max(320, int(float(self.browser_window_vars["w"].get() or 1200)))
            self.config.browser_window_h = max(240, int(float(self.browser_window_vars["h"].get() or 900)))
            self.config.browser_wait_seconds = max(0.0, float(self.browser_wait_seconds_var.get() or 2))
            self.config.browser_next_wait_seconds = max(0.0, float(self.browser_next_wait_seconds_var.get() or 2))
            self.config.browser_urls = parse_url_lines(self.browser_urls_input.get("1.0", END) if hasattr(self, "browser_urls_input") else self.config.browser_urls)
            self.config.workflow_actions = normalize_workflow_actions(self.workflow_actions)
            save_config(self.config)
            if self.browser_controller and self.browser_controller.config is not self.config:
                self.browser_controller = None
            if self.worker.config is not self.config or old_lang != self.config.paddle_lang:
                self.worker = PaddleWorker(self.config)
            elif old_cache_key != self.worker.cache_key():
                self.worker.invalidate_cache()
            self.status_var.set("设置已保存")
            return True
        except Exception as exc:
            self.report_error("设置错误", exc)
            return False

    def pick_region(self) -> None:
        self.save_from_ui()
        image = self.worker.screenshot()
        origin_x, origin_y, screen_w, screen_h = virtual_screen_bounds()
        self.append_output(f"区域选择器已打开\n虚拟桌面: origin=({origin_x}, {origin_y}), size={screen_w}x{screen_h}\n截图尺寸: {image.size[0]}x{image.size[1]}\n{'-' * 48}")
        RegionPicker(self.root, image, self.on_region_picked)

    def on_region_picked(self, region: Region) -> None:
        self.update_region_ui(region)
        self.save_from_ui()
        self.append_output(f"已选择 OCR 区域\n截图坐标: x={region.x}, y={region.y}, w={region.w}, h={region.h}\n{'-' * 48}")
        self.preview_region()

    def capture_region_corner_after_delay(self, corner: str) -> None:
        label = "左上角" if corner == "tl" else "右下角"
        self.append_output(f"请在 3 秒内把鼠标移动到目标区域{label}...\n{'-' * 48}")
        self.root.iconify()
        self.root.after(3000, lambda: self.capture_region_corner(corner))

    def capture_region_corner(self, corner: str) -> None:
        screen_x, screen_y = cursor_position()
        image = self.worker.screenshot()
        image_x, image_y = self.worker.screen_point_to_image_point(screen_x, screen_y, image)
        self.region_corner_points[corner] = (image_x, image_y)
        self.root.deiconify()
        self.root.lift()
        if "tl" in self.region_corner_points and "br" in self.region_corner_points:
            x1, y1 = self.region_corner_points["tl"]
            x2, y2 = self.region_corner_points["br"]
            left, right = sorted((x1, x2))
            top, bottom = sorted((y1, y2))
            self.on_region_picked(Region("target", left, top, max(1, right - left), max(1, bottom - top)))
            self.region_corner_points.clear()
        else:
            self.status_var.set("已记录一个角点，请继续记录另一个角点")

    def preview_region(self) -> None:
        if not self.save_from_ui():
            return
        try:
            image = self.worker.crop_region(self.worker.screenshot())
            self.update_preview_image(image)
        except Exception as exc:
            self.report_error("截图失败", exc)

    def update_preview_image(self, image: Image.Image) -> None:
        display = image.copy()
        display.thumbnail((940, 620))
        self.preview_photo = ImageTk.PhotoImage(display)
        self.preview_label.configure(image=self.preview_photo, text="")

    def show_located_item(self, details: dict, item: dict, title: str) -> None:
        self.update_located_preview(details, item)
        cache_text = "缓存命中" if details.get("cache_hit") else "新 OCR"
        self.append_output(
            f"{title}\n"
            f"命中文字：{item.get('text', '')}\n"
            f"屏幕中心：{item.get('screen_center_x')}, {item.get('screen_center_y')}\n"
            f"来源：{cache_text}\n"
            f"{'-' * 48}"
        )

    def update_located_preview(self, details: dict, item: dict) -> None:
        annotated = self.worker.annotate_ocr_image(details["image"], details["items"], highlight_item=item)
        self.update_preview_image(annotated)
        self.position_var.set(f"命中：{item.get('text', '')} -> {item.get('screen_center_x')}, {item.get('screen_center_y')}")

    def workflow_event(self, event: str, payload: dict) -> None:
        self.root.after(0, lambda event=event, payload=payload: self.handle_workflow_event(event, payload))

    def toggle_settings_panel(self) -> None:
        if not hasattr(self, "main_pane") or not hasattr(self, "content_pane"):
            return
        if self.settings_collapsed:
            try:
                self.main_pane.insert(0, self.content_pane, weight=5)
            except Exception:
                self.main_pane.add(self.content_pane, weight=5)
            self.settings_collapsed = False
            self.collapse_button.configure(text="收起设置")
            self.status_var.set("已展开设置区")
        else:
            try:
                self.main_pane.forget(self.content_pane)
            except Exception:
                return
            self.settings_collapsed = True
            self.collapse_button.configure(text="展开设置")
            self.status_var.set("已收起设置区，运行结果保留显示")

    def handle_workflow_event(self, event: str, payload: dict) -> None:
        index = payload.get("index")
        if event == "step_start" and index is not None:
            self.executing_step_index = index
            self.mark_workflow_step(index, "running")
            action = payload.get("action") or {}
            self.status_var.set(f"正在执行第 {index + 1} 步：{ACTION_TYPES.get(action.get('type'), action.get('type', ''))}")
        elif event == "step_done" and index is not None:
            self.mark_workflow_step(index, "done")
        elif event == "step_skip" and index is not None:
            self.mark_workflow_step(index, "skip")
        elif event == "ocr":
            details = payload.get("details")
            if details:
                annotated = self.worker.annotate_ocr_image(details["image"], details["items"])
                self.update_preview_image(annotated)
        elif event == "located":
            result = payload.get("result") or {}
            details = result.get("details")
            item = result.get("item")
            if details and item:
                self.update_located_preview(details, item)
        elif event == "similarity":
            comparison = payload.get("comparison") or {}
            self.refresh_similarity_debug_view()
            if comparison.get("available"):
                self.status_var.set(
                    f"前景相似度 {comparison.get('similarity', 0):.1f}% / 阈值 {comparison.get('threshold', 90):.1f}% / 回看 {comparison.get('lag', 1)} 张"
                )
            else:
                self.status_var.set(comparison.get("reason") or "已记录截图用于相似度判断")
        elif event == "answer_validation_failed":
            if index is not None:
                self.mark_workflow_step(index, "error")
            validation = payload.get("validation") or {}
            self.status_var.set(f"答案文本校验失败：{validation.get('reason') or '已暂停'}")
            self.append_output(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 答案文本校验失败\n"
                f"=>\treason: {validation.get('reason') or '已暂停'}\n"
                f"{'-' * 48}",
                tag="error",
            )

    def update_run_buttons(self) -> None:
        if not hasattr(self, "start_button"):
            return
        self.start_button.configure(state=DISABLED if (self.running or self.busy) else NORMAL)
        if self.running and self.browser_batch_active():
            self.start_button.configure(text="网址处理中")
        else:
            self.start_button.configure(text="循环中" if self.running else "开始循环")
        self.pause_button.configure(state=NORMAL if self.running else DISABLED)
        self.pause_button.configure(text="继续" if self.paused else "暂停")
        self.stop_button.configure(state=NORMAL if (self.running or self.busy) else DISABLED)

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.update_run_buttons()
        if self.paused:
            self.status_var.set("已暂停")
        elif self.running and self.browser_batch_active():
            self.status_var.set(f"网址 {self.browser_batch_label()} 运行中..." if busy else f"等待下一轮：网址 {self.browser_batch_label()}")
        else:
            self.status_var.set("运行中..." if busy else ("循环中" if self.running else "就绪"))

    def append_output(self, text: str, tag: str | None = None) -> None:
        self.output.insert(END, text + "\n", tag)
        self.output.see(END)

    def report_error(self, title: str, error, detail: str = "") -> None:
        message = str(error or "").strip() or "(无错误信息)"
        self.status_var.set(f"{title}：{message[:80]}")
        body = (
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {title}\n"
            f"=>\terror: {message}\n"
        )
        if detail:
            body += f"=>\tdetail: {detail.strip()}\n"
        body += "-" * 48
        self.append_output(body, tag="error")

    def browser_batch_active(self) -> bool:
        return bool(getattr(self.config, "browser_enabled", False) and getattr(self.config, "browser_urls", []))

    def browser_wait_ms(self) -> int:
        return max(0, int(float(getattr(self.config, "browser_wait_seconds", 2)) * 1000))

    def browser_next_wait_ms(self) -> int:
        return max(0, int(float(getattr(self.config, "browser_next_wait_seconds", 2)) * 1000))

    def browser_batch_label(self) -> str:
        total = len(self.config.browser_urls)
        if not total:
            return "未配置网址"
        return f"{self.browser_url_index + 1}/{total}"

    def start_browser_batch_async(self) -> None:
        self.browser_url_index = 0
        self.current_browser_url = self.config.browser_urls[0]
        self.worker.reset_similarity_reference()
        self.worker.invalidate_cache()
        self.batch_status_var.set(f"正在启动浏览器：{self.browser_batch_label()}")
        self.set_busy(True)
        self.status_var.set("正在启动浏览器...")
        threading.Thread(target=self._start_browser_batch_thread, daemon=True).start()

    def _start_browser_batch_thread(self) -> None:
        try:
            if self.browser_controller is None:
                self.browser_controller = BrowserController(self.config)
            self.browser_controller.open_url(self.current_browser_url)

            def done():
                self.append_output(
                    f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 浏览器已打开\n"
                    f"网址 {self.browser_batch_label()}：{self.current_browser_url}\n"
                    f"{'-' * 48}"
                )
                self.batch_status_var.set(f"当前网址 {self.browser_batch_label()}：{self.current_browser_url}")
                self.set_busy(False)
                if self.running and not self.stop_event.is_set():
                    self.root.after(self.browser_wait_ms(), self.run_once_async)

            self.root.after(0, done)
        except Exception as exc:
            error_text = str(exc)
            self.root.after(0, lambda error_text=error_text: self.report_error("浏览器启动失败", error_text))
            self.root.after(0, lambda: self.set_busy(False))
            if self.running and not self.stop_event.is_set():
                self.root.after(self.config.interval_seconds * 1000, self.start_browser_batch_async)

    def schedule_after_workflow(self, record: dict | None, failed: bool = False) -> None:
        if record and record.get("answer_validation_failed"):
            reason = record.get("answer_validation_error") or "答案文本与 OCR 选项不一致"
            self.validation_pause_pending = bool(self.running and not self.stop_event.is_set())
            if self.validation_pause_pending:
                self.paused = True
                self.pause_event.clear()
            self.status_var.set(f"答案文本校验失败，已暂停：{reason}")
            if self.browser_batch_active():
                self.batch_status_var.set("答案文本校验失败，已暂停网址批处理")
            self.update_run_buttons()
            self.append_output(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 答案文本校验失败，已暂停\n"
                f"=>\treason: {reason}\n"
                f"=>\taction: 未点击答案，请检查 AI 返回和 OCR 选项文本\n"
                f"{'-' * 48}",
                tag="error",
            )
            return
        if not self.running or self.stop_event.is_set():
            return
        if self.browser_batch_active() and record and record.get("screenshot_similarity_hit"):
            similarity = record.get("screenshot_similarity")
            suffix = f"，前景相似度 {similarity:.1f}%" if isinstance(similarity, (int, float)) else ""
            self.batch_status_var.set(f"当前网址处理完成{suffix}，准备跳转")
            self.append_output(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 当前网址处理完成{suffix}\n"
                f"网址 {self.browser_batch_label()}：{self.current_browser_url}\n"
                f"{'-' * 48}"
            )
            self.root.after(self.browser_next_wait_ms(), self.advance_browser_url_async)
            return
        delay = self.config.interval_seconds * 1000
        if failed:
            self.batch_status_var.set("本轮执行失败，等待下一轮")
        self.root.after(delay, self.run_once_async)

    def advance_browser_url_async(self) -> None:
        if not self.running or self.stop_event.is_set():
            return
        self.browser_url_index += 1
        if self.browser_url_index >= len(self.config.browser_urls):
            self.running = False
            self.stop_event.set()
            self.batch_status_var.set("全部网址处理完成")
            self.status_var.set("全部网址处理完成")
            self.update_run_buttons()
            self.append_output(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 全部网址处理完成\n{'-' * 48}")
            return
        self.current_browser_url = self.config.browser_urls[self.browser_url_index]
        self.worker.reset_similarity_reference()
        self.worker.invalidate_cache()
        self.batch_status_var.set(f"正在跳转网址 {self.browser_batch_label()}")
        self.set_busy(True)
        self.status_var.set(f"正在跳转网址 {self.browser_batch_label()}...")
        threading.Thread(target=self._navigate_browser_url_thread, daemon=True).start()

    def _navigate_browser_url_thread(self) -> None:
        try:
            if self.browser_controller is None:
                self.browser_controller = BrowserController(self.config)
            self.browser_controller.open_url(self.current_browser_url)

            def done():
                self.append_output(
                    f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 已跳转到下一个网址\n"
                    f"网址 {self.browser_batch_label()}：{self.current_browser_url}\n"
                    f"{'-' * 48}"
                )
                self.batch_status_var.set(f"当前网址 {self.browser_batch_label()}：{self.current_browser_url}")
                self.set_busy(False)
                if self.running and not self.stop_event.is_set():
                    self.root.after(self.browser_wait_ms(), self.run_once_async)

            self.root.after(0, done)
        except Exception as exc:
            error_text = str(exc)
            self.root.after(0, lambda error_text=error_text: self.report_error("浏览器跳转失败", error_text))
            self.root.after(0, lambda: self.set_busy(False))
            if self.running and not self.stop_event.is_set():
                self.root.after(self.config.interval_seconds * 1000, self.retry_current_browser_url_async)

    def retry_current_browser_url_async(self) -> None:
        if not self.running or self.stop_event.is_set():
            return
        self.batch_status_var.set(f"重试跳转网址 {self.browser_batch_label()}")
        self.set_busy(True)
        threading.Thread(target=self._navigate_browser_url_thread, daemon=True).start()

    def refresh_ocr_cache_async(self) -> None:
        if self.busy or not self.save_from_ui():
            return
        self.set_busy(True)
        threading.Thread(target=self._refresh_ocr_cache_thread, daemon=True).start()

    def _refresh_ocr_cache_thread(self) -> None:
        try:
            details = self.worker.ocr_region_details(force=True)
            annotated = self.worker.annotate_ocr_image(details["image"], details["items"])

            def done():
                self.update_preview_image(annotated)
                self.append_output(
                    f"[{details['time']}] OCR 缓存已刷新：识别到 {len(details['items'])} 个文字块\n"
                    f"{'-' * 48}"
                )

            self.root.after(0, done)
        except Exception as exc:
            error_text = str(exc)
            self.root.after(0, lambda error_text=error_text: self.report_error("刷新 OCR 缓存失败", error_text))
        finally:
            self.root.after(0, lambda: self.set_busy(False))

    def run_once_async(self) -> None:
        if self.busy or not self.save_from_ui():
            return
        loop_invocation = self.running
        if loop_invocation:
            if self.stop_event.is_set():
                return
        else:
            self.stop_event.clear()
            self.pause_event.set()
            self.paused = False
        self.executing_step_index = None
        self.clear_workflow_step_marks()
        self.set_busy(True)
        threading.Thread(target=lambda: self._run_once_thread(loop_invocation), daemon=True).start()

    def _run_once_thread(self, loop_invocation: bool = False) -> None:
        record = None
        failed = False
        try:
            record = self.worker.execute_workflow(
                self.config.workflow_actions,
                should_stop=(lambda: self.stop_event.is_set() or (loop_invocation and not self.running)),
                should_pause=(lambda: not self.pause_event.is_set()),
                on_event=self.workflow_event,
                stop_on_similarity=self.browser_batch_active(),
            )

            def done(record=record):
                logs = "\n".join(record.get("workflow_logs") or [])
                matches = ",".join(record.get("matches") or [])
                similarity = record.get("screenshot_similarity")
                similarity_text = f"\n前景相似度: {similarity:.1f}%" if isinstance(similarity, (int, float)) else ""
                url_text = f"网址 {self.browser_batch_label()}：{self.current_browser_url}\n" if self.browser_batch_active() else ""
                self.append_output(
                    f"[{record['time']}]\n"
                    f"{url_text}"
                    f"流程:\n{logs or '(无动作)'}\n\n"
                    f"Q/OCR:\n{record.get('q') or '(空)'}\n\n"
                    f"AI:\n{record.get('answer') or '(空)'}\n\n"
                    f"解析结果: {matches or '(空)'}{similarity_text}\n"
                    f"{'-' * 48}"
                )

            self.root.after(0, done)
        except Exception as exc:
            failed = True
            error_text = str(exc)
            error_detail = traceback.format_exc(limit=6)
            if self.executing_step_index is not None:
                self.root.after(0, lambda index=self.executing_step_index: self.mark_workflow_step(index, "error"))
            self.root.after(0, lambda error_text=error_text, error_detail=error_detail: self.report_error("执行失败", error_text, error_detail))
        finally:
            self.root.after(0, lambda record=record, failed=failed: (self.set_busy(False), self.schedule_after_workflow(record, failed)))

    def visualize_ocr_async(self) -> None:
        if self.busy or not self.save_from_ui():
            return
        self.set_busy(True)
        threading.Thread(target=self._visualize_ocr_thread, daemon=True).start()

    def _visualize_ocr_thread(self) -> None:
        try:
            details = self.worker.ocr_region_details(force=False)
            annotated = self.worker.annotate_ocr_image(details["image"], details["items"])

            def show_window():
                self.update_preview_image(annotated)
                OcrVisualizerWindow(self.root, details, annotated)
                cache_text = "缓存命中" if details.get("cache_hit") else "新 OCR"
                self.append_output(
                    f"[{details['time']}] PaddleOCR 可视化完成：识别到 {len(details['items'])} 个文字块\n"
                    f"来源：{cache_text}\n"
                    f"{'-' * 48}"
                )

            self.root.after(0, show_window)
        except Exception as exc:
            error_text = str(exc)
            self.root.after(0, lambda error_text=error_text: self.report_error("OCR 可视化失败", error_text))
        finally:
            self.root.after(0, lambda: self.set_busy(False))

    def test_ai_async(self) -> None:
        if self.busy or not self.save_from_ui():
            return
        self.set_busy(True)
        threading.Thread(target=self._test_ai_thread, daemon=True).start()

    def _test_ai_thread(self) -> None:
        try:
            result = self.worker.test_ai_connectivity()
            status = "成功" if result.get("ok") else "失败"
            msg = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] AI 连通性测试：{status}\nHTTP: {result.get('http_status') or '-'}\n耗时: {result.get('elapsed_ms') or '-'} ms\n返回: {result.get('answer') or result.get('error') or ''}\n{'-' * 48}"
            self.root.after(0, lambda msg=msg, status=status: self.append_output(msg, tag="error" if status == "失败" else None))
        except Exception as exc:
            error_text = str(exc)
            self.root.after(0, lambda error_text=error_text: self.report_error("AI 连通性测试异常", error_text))
        finally:
            self.root.after(0, lambda: self.set_busy(False))

    def toggle_loop(self) -> None:
        if self.running:
            self.status_var.set("循环已经在运行")
            return
        if not self.save_from_ui():
            return
        self.stop_event.clear()
        self.pause_event.set()
        self.paused = False
        self.validation_pause_pending = False
        self.running = True
        self.clear_workflow_step_marks()
        self.update_run_buttons()
        if self.browser_batch_active():
            self.append_output(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 开始网址批处理，共 {len(self.config.browser_urls)} 个网址\n"
                f"{'-' * 48}"
            )
            self.start_browser_batch_async()
        else:
            self.status_var.set("循环中")
            self.run_once_async()

    def toggle_pause(self) -> None:
        if not self.running:
            return
        resume_validation_pause = self.paused and self.validation_pause_pending and not self.busy
        self.paused = not self.paused
        if self.paused:
            self.pause_event.clear()
            self.status_var.set("已暂停，点击“继续”恢复")
        else:
            self.pause_event.set()
            self.status_var.set("继续执行")
            if resume_validation_pause:
                self.validation_pause_pending = False
                self.root.after(0, self.run_once_async)
        self.update_run_buttons()

    def stop_loop(self) -> None:
        if not (self.running or self.busy):
            return
        self.running = False
        self.paused = False
        self.validation_pause_pending = False
        self.stop_event.set()
        self.pause_event.set()
        if self.browser_batch_active():
            self.batch_status_var.set("已停止网址批处理")
        self.status_var.set("正在停止..." if self.busy else "已停止")
        self.update_run_buttons()

    def locate_text_async(self) -> None:
        self._text_action_async(click=False)

    def click_text_async(self) -> None:
        self._text_action_async(click=True)

    def locate_choice_async(self, letter: str) -> None:
        self._choice_action_async(letter, click=False)

    def click_choice_async(self, letter: str) -> None:
        self._choice_action_async(letter, click=True)

    def _choice_action_async(self, letter: str, click: bool) -> None:
        if not self.save_from_ui():
            return
        self.status_var.set(f"{'点击' if click else '定位'}选项 {letter} 中...")

        def work():
            try:
                result = self.worker.locate_choice(letter, click_area="letter")
                if result:
                    pos = result["pos"]
                    if click:
                        self.worker.click_position(*pos)
                    msg = f"{'已点击' if click else '已定位'}选项 {letter} -> {pos[0]}, {pos[1]}"

                    def done(result=result, msg=msg):
                        self.position_var.set(msg)
                        self.status_var.set("就绪")
                        self.show_located_item(result["details"], result["item"], msg)

                    self.root.after(0, done)
                else:
                    msg = f"未找到选项 {letter}"

                    def not_found(msg=msg):
                        self.position_var.set(msg)
                        self.status_var.set("就绪")
                        self.append_output(f"{msg}\n{'-' * 48}")

                    self.root.after(0, not_found)
            except Exception as exc:
                error_text = str(exc)
                self.root.after(0, lambda error_text=error_text: self.report_error("选项操作失败", error_text))
                self.root.after(0, lambda: self.status_var.set("就绪"))

        threading.Thread(target=work, daemon=True).start()

    def _text_action_async(self, click: bool) -> None:
        text = self.find_text_var.get().strip()
        if not text:
            self.report_error("缺少文字", "请输入要定位的文字。")
            return
        if not self.save_from_ui():
            return
        self.status_var.set(f"{'点击' if click else '定位'}文字中...")

        def work():
            try:
                result = self.worker.locate_target(text, click_area="letter")
                if result:
                    pos = result["pos"]
                    if click:
                        self.worker.click_position(*pos)
                    msg = f"{'已点击' if click else '已定位'}：{text} -> {pos[0]}, {pos[1]}"

                    def done(result=result, msg=msg):
                        self.position_var.set(msg)
                        self.status_var.set("就绪")
                        self.show_located_item(result["details"], result["item"], msg)

                    self.root.after(0, done)
                else:
                    msg = f"未找到：{text}"

                    def not_found(msg=msg):
                        self.position_var.set(msg)
                        self.status_var.set("就绪")
                        self.append_output(f"{msg}\n{'-' * 48}")

                    self.root.after(0, not_found)
            except Exception as exc:
                error_text = str(exc)
                self.root.after(0, lambda error_text=error_text: self.report_error("定位失败", error_text))
                self.root.after(0, lambda: self.status_var.set("就绪"))

        threading.Thread(target=work, daemon=True).start()

    def on_close(self) -> None:
        self.running = False
        self.save_from_ui()
        if self.browser_controller is not None:
            self.browser_controller.close()
        self.root.destroy()


def main() -> None:
    enable_dpi_awareness()
    root = Tk()
    root.geometry("1080x940")
    root.minsize(760, 620)
    PaddleApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

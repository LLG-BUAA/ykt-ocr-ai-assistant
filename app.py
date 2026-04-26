import ctypes
import ctypes
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from dataclasses import field
from pathlib import Path
from tkinter import BOTH, BooleanVar, DISABLED, END, LEFT, NORMAL, RIGHT, Canvas, StringVar, Text, Tk, Toplevel, filedialog, messagebox, ttk
import winreg

from PIL import Image, ImageDraw, ImageFilter, ImageGrab, ImageOps, ImageTk

try:
    import pytesseract
except ImportError:  # The UI will show a clear install hint.
    pytesseract = None


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
SNAPSHOT_DIR = APP_DIR / "snapshots"
RESULTS_PATH = APP_DIR / "results.jsonl"


def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def virtual_screen_origin() -> tuple[int, int]:
    return (
        int(ctypes.windll.user32.GetSystemMetrics(76)),
        int(ctypes.windll.user32.GetSystemMetrics(77)),
    )


class Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def cursor_position() -> tuple[int, int]:
    point = Point()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
    return int(point.x), int(point.y)


def virtual_screen_bounds() -> tuple[int, int, int, int]:
    return (
        int(ctypes.windll.user32.GetSystemMetrics(76)),
        int(ctypes.windll.user32.GetSystemMetrics(77)),
        int(ctypes.windll.user32.GetSystemMetrics(78)),
        int(ctypes.windll.user32.GetSystemMetrics(79)),
    )


def move_window_absolute(window: Toplevel, x: int, y: int, width: int, height: int) -> None:
    window.update_idletasks()
    hwnd = int(window.winfo_id())
    hwnd_topmost = -1
    swp_showwindow = 0x0040
    ctypes.windll.user32.SetWindowPos(hwnd, hwnd_topmost, int(x), int(y), int(width), int(height), swp_showwindow)


def registry_tesseract_paths() -> list[str]:
    paths: list[str] = []
    uninstall_roots = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    for hive, root in uninstall_roots:
        try:
            with winreg.OpenKey(hive, root) as root_key:
                for index in range(winreg.QueryInfoKey(root_key)[0]):
                    try:
                        subkey_name = winreg.EnumKey(root_key, index)
                        with winreg.OpenKey(root_key, subkey_name) as subkey:
                            display_name = str(winreg.QueryValueEx(subkey, "DisplayName")[0])
                            if "tesseract" not in display_name.lower():
                                continue
                            for value_name in ("InstallLocation", "DisplayIcon", "UninstallString"):
                                try:
                                    value = str(winreg.QueryValueEx(subkey, value_name)[0]).strip('"')
                                except OSError:
                                    continue
                                candidate_dir = Path(value)
                                if candidate_dir.suffix:
                                    candidate_dir = candidate_dir.parent
                                paths.append(str(candidate_dir / "tesseract.exe"))
                    except OSError:
                        continue
        except OSError:
            continue
    return paths


def detect_tesseract_cmd() -> str:
    detected = shutil.which("tesseract")
    if detected:
        return detected
    candidates = [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        r"D:\Workstation\Tesseract-OCR\tesseract.exe",
    ]
    candidates.extend(registry_tesseract_paths())
    for candidate in dict.fromkeys(candidates):
        if Path(candidate).exists():
            return candidate
    return ""


def tesseract_languages(tesseract_cmd: str) -> list[str]:
    if not tesseract_cmd or not Path(tesseract_cmd).exists():
        return []
    try:
        completed = subprocess.run(
            [tesseract_cmd, "--list-langs"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return []
    lines = completed.stdout.splitlines() + completed.stderr.splitlines()
    return sorted({line.strip() for line in lines if line.strip() and not line.startswith("List of available")})


def missing_ocr_languages(tesseract_cmd: str, requested: str) -> list[str]:
    installed = set(tesseract_languages(tesseract_cmd))
    requested_langs = [item.strip() for item in requested.split("+") if item.strip()]
    return [lang for lang in requested_langs if lang not in installed]


@dataclass
class Region:
    name: str = "target"
    x: int = 0
    y: int = 0
    w: int = 800
    h: int = 400

    def box(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.x + self.w, self.y + self.h)


@dataclass
class AppConfig:
    interval_seconds: int = 3
    region: Region = field(default_factory=Region)
    prompt_u: str = "请根据下面 OCR 得到的文字，提取关键信息并给出简洁判断："
    api_base_url: str = "http://192.168.31.114:7999/v1beta"
    gemini_model: str = "gemini-3-pro-preview"
    api_key_env: str = "SUB2API_API_KEY"
    api_key: str = ""
    tesseract_cmd: str = ""
    ocr_lang: str = "chi_sim+eng"
    preprocess_grayscale: bool = True
    preprocess_autocontrast: bool = True
    preprocess_binarize: bool = True
    binary_threshold: int = 175
    preprocess_invert: bool = False
    preprocess_scale: int = 2
    preprocess_sharpen: bool = True
    ocr_psm_modes: str = "6,11"
    ocr_multi_pass: bool = True
    choice_enhance: bool = True
    save_snapshots: bool = True


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_config() -> AppConfig:
    load_dotenv(APP_DIR / ".env")
    if not CONFIG_PATH.exists():
        config = AppConfig()
        config.tesseract_cmd = detect_tesseract_cmd()
        return config
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    region = Region(**raw.get("region", {}))
    raw["region"] = region
    config = AppConfig(**raw)
    if not config.tesseract_cmd or not Path(config.tesseract_cmd).exists():
        config.tesseract_cmd = detect_tesseract_cmd()
    return config


def save_config(config: AppConfig) -> None:
    data = asdict(config)
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class ScreenGeminiWorker:
    def __init__(self, config: AppConfig):
        self.config = config
        if not self.config.tesseract_cmd:
            self.config.tesseract_cmd = detect_tesseract_cmd()
        if pytesseract and config.tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = config.tesseract_cmd

    def screenshot(self) -> Image.Image:
        return ImageGrab.grab(all_screens=True)

    def crop_region(self, image: Image.Image, region: Region | None = None) -> Image.Image:
        region = region or self.config.region
        return image.crop(region.box())

    def screen_point_to_image_point(self, x: int, y: int, image: Image.Image | None = None) -> tuple[int, int]:
        image = image or self.screenshot()
        origin_x, origin_y, screen_w, screen_h = virtual_screen_bounds()
        scale_x = image.size[0] / max(1, screen_w)
        scale_y = image.size[1] / max(1, screen_h)
        image_x = int(round((x - origin_x) * scale_x))
        image_y = int(round((y - origin_y) * scale_y))
        image_x = max(0, min(image_x, image.size[0] - 1))
        image_y = max(0, min(image_y, image.size[1] - 1))
        return image_x, image_y

    def validate_ocr_setup(self) -> None:
        if pytesseract is None:
            raise RuntimeError("缺少 pytesseract，请先执行：pip install -r requirements.txt")
        if self.config.tesseract_cmd and not Path(self.config.tesseract_cmd).exists():
            raise RuntimeError(f"Tesseract 路径不存在：{self.config.tesseract_cmd}")
        missing = missing_ocr_languages(self.config.tesseract_cmd, self.config.ocr_lang)
        if missing:
            installed = ", ".join(tesseract_languages(self.config.tesseract_cmd)) or "无"
            raise RuntimeError(
                f"Tesseract 缺少语言包：{', '.join(missing)}\n"
                f"当前已安装语言：{installed}\n"
                "请安装对应 .traineddata，或把 OCR 语言改成已安装的语言，例如 eng。"
            )

    def ocr_psm_list(self) -> list[int]:
        modes: list[int] = []
        for raw in self.config.ocr_psm_modes.split(","):
            raw = raw.strip()
            if not raw:
                continue
            try:
                mode = int(raw)
            except ValueError:
                continue
            if 0 <= mode <= 13 and mode not in modes:
                modes.append(mode)
        return modes or [6]

    def tesseract_config(self, psm: int) -> str:
        return f"--oem 3 --psm {psm} -c preserve_interword_spaces=1"

    def preprocess_ocr_image(self, image: Image.Image, force_binarize: bool | None = None) -> tuple[Image.Image, float]:
        processed = image
        use_binarize = self.config.preprocess_binarize if force_binarize is None else force_binarize
        if self.config.preprocess_grayscale or use_binarize:
            processed = ImageOps.grayscale(processed)
        if self.config.preprocess_autocontrast:
            if processed.mode != "L":
                processed = ImageOps.grayscale(processed)
            processed = ImageOps.autocontrast(processed)
        if self.config.preprocess_invert:
            if processed.mode != "L":
                processed = ImageOps.grayscale(processed)
            processed = ImageOps.invert(processed)
        scale = max(1, min(4, int(self.config.preprocess_scale)))
        if scale > 1:
            processed = processed.resize((processed.width * scale, processed.height * scale), Image.Resampling.LANCZOS)
        if self.config.preprocess_sharpen:
            processed = processed.filter(ImageFilter.SHARPEN)
        if use_binarize:
            if processed.mode != "L":
                processed = ImageOps.grayscale(processed)
            threshold = max(0, min(255, int(self.config.binary_threshold)))
            processed = processed.point(lambda value: 255 if value >= threshold else 0, mode="L")
        return processed, float(scale)

    def preprocess_summary(self) -> str:
        steps = []
        if self.config.preprocess_grayscale:
            steps.append("灰度")
        if self.config.preprocess_autocontrast:
            steps.append("自动对比度")
        if self.config.preprocess_binarize:
            steps.append(f"二值化({self.config.binary_threshold})")
        if self.config.preprocess_invert:
            steps.append("反色")
        if self.config.preprocess_scale > 1:
            steps.append(f"放大{self.config.preprocess_scale}x")
        if self.config.preprocess_sharpen:
            steps.append("锐化")
        if self.config.ocr_multi_pass:
            steps.append(f"多模式PSM({self.config.ocr_psm_modes})")
        else:
            steps.append(f"PSM({self.ocr_psm_list()[0]})")
        return " + ".join(steps) if steps else "无"

    def merge_ocr_texts(self, texts: list[str]) -> str:
        lines: list[str] = []
        seen: set[str] = set()
        for text in texts:
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                key = " ".join(line.split())
                if key not in seen:
                    seen.add(key)
                    lines.append(line)
        return "\n".join(lines).strip()

    def compact_ocr_text(self, text: str) -> str:
        return " ".join(text.replace("\n", " ").split()).strip()

    def text_score(self, text: str) -> tuple[int, int, int]:
        cjk_count = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
        alnum_count = sum(1 for char in text if char.isalnum())
        noise_count = sum(1 for char in text if not char.isalnum() and char not in ".。:：、 ")
        return cjk_count, alnum_count, -noise_count

    def clean_choice_text(self, text: str) -> str:
        text = self.compact_ocr_text(text).replace("|", "").strip()
        cjk_chars = "".join(char for char in text if "\u4e00" <= char <= "\u9fff")
        if cjk_chars:
            return cjk_chars
        if text in {"+", "十"}:
            return "土"
        return text

    def ocr_image(self, image: Image.Image) -> str:
        self.validate_ocr_setup()
        if self.config.tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = self.config.tesseract_cmd
        processed, _scale = self.preprocess_ocr_image(image)
        modes = self.ocr_psm_list()
        if not self.config.ocr_multi_pass:
            modes = modes[:1]
        texts = [
            pytesseract.image_to_string(processed, lang=self.config.ocr_lang, config=self.tesseract_config(psm)).strip()
            for psm in modes
        ]
        return self.merge_ocr_texts(texts)

    def ocr_short_text(self, image: Image.Image) -> str:
        self.validate_ocr_setup()
        if self.config.tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = self.config.tesseract_cmd
        candidates: list[str] = []
        variants = [self.preprocess_ocr_image(image)]
        if self.config.preprocess_binarize:
            variants.append(self.preprocess_ocr_image(image, force_binarize=False))
        for processed, _scale in variants:
            for psm in (6, 7, 10):
                text = pytesseract.image_to_string(
                    processed,
                    lang=self.config.ocr_lang,
                    config=self.tesseract_config(psm),
                ).strip()
                cleaned = self.clean_choice_text(text)
                if cleaned:
                    candidates.append(cleaned)
        if not candidates:
            return ""
        return max(candidates, key=self.text_score)

    def ocr_items(self, image: Image.Image, region: Region | None = None) -> list[dict]:
        self.validate_ocr_setup()
        if self.config.tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = self.config.tesseract_cmd
        region = region or self.config.region
        processed, scale = self.preprocess_ocr_image(image)
        origin_x, origin_y = virtual_screen_origin()
        items: list[dict] = []
        modes = self.ocr_psm_list()
        if not self.config.ocr_multi_pass:
            modes = modes[:1]
        for psm in modes:
            data = pytesseract.image_to_data(
                processed,
                lang=self.config.ocr_lang,
                config=self.tesseract_config(psm),
                output_type=pytesseract.Output.DICT,
            )
            for index, text in enumerate(data.get("text", [])):
                text = text.strip()
                if not text:
                    continue
                left = int(round(int(data["left"][index]) / scale))
                top = int(round(int(data["top"][index]) / scale))
                width = max(1, int(round(int(data["width"][index]) / scale)))
                height = max(1, int(round(int(data["height"][index]) / scale)))
                if width <= 0 or height <= 0:
                    continue
                conf_raw = data.get("conf", [""])[index]
                try:
                    confidence = round(float(conf_raw), 1)
                except (TypeError, ValueError):
                    confidence = None
                if self.is_duplicate_ocr_item(items, text, left, top, width, height):
                    continue
                screen_left = origin_x + region.x + left
                screen_top = origin_y + region.y + top
                items.append(
                    {
                        "index": len(items) + 1,
                        "text": text,
                        "confidence": confidence,
                        "psm": psm,
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

    def is_duplicate_ocr_item(self, items: list[dict], text: str, left: int, top: int, width: int, height: int) -> bool:
        center_x = left + width // 2
        center_y = top + height // 2
        for item in items:
            same_text = item["text"] == text
            close_center = abs(item["left"] + item["width"] // 2 - center_x) <= 6 and abs(
                item["top"] + item["height"] // 2 - center_y
            ) <= 6
            if same_text and close_center:
                return True
        return False

    def is_marker_pixel(self, pixel: tuple[int, int, int]) -> bool:
        r, g, b = pixel
        blue_marker = b > 150 and r < 130 and g < 180
        gray_or_dark_marker = max(r, g, b) < 235 and min(r, g, b) < 225
        return blue_marker or gray_or_dark_marker

    def detect_choice_markers(self, image: Image.Image) -> list[dict]:
        rgb = image.convert("RGB")
        width, height = rgb.size
        scan_width = min(width, min(120, max(72, int(width * 0.13))))
        candidates: list[dict] = []

        row_counts = []
        for y in range(height):
            row_counts.append(sum(1 for x in range(scan_width) if self.is_marker_pixel(rgb.getpixel((x, y)))))

        bands: list[tuple[int, int]] = []
        start = None
        gap = 0
        for y, count in enumerate(row_counts):
            if count >= 2:
                if start is None:
                    start = y
                gap = 0
            elif start is not None:
                gap += 1
                if gap > 5:
                    bands.append((start, y - gap))
                    start = None
                    gap = 0
        if start is not None:
            bands.append((start, height - 1))

        for top, bottom in bands:
            band_h = bottom - top + 1
            if not 24 <= band_h <= 95:
                continue
            xs = []
            ys = []
            for y in range(top, bottom + 1):
                for x in range(scan_width):
                    if self.is_marker_pixel(rgb.getpixel((x, y))):
                        xs.append(x)
                        ys.append(y)
            if not xs:
                continue
            min_x, max_x = min(xs), max(xs)
            min_y, max_y = min(ys), max(ys)
            box_w = max_x - min_x + 1
            box_h = max_y - min_y + 1
            aspect = box_w / max(1, box_h)
            area = len(xs)
            if 28 <= box_w <= 92 and 28 <= box_h <= 95 and 0.55 <= aspect <= 1.55 and area >= 70:
                candidates.append(
                    {
                        "left": min_x,
                        "top": min_y,
                        "width": box_w,
                        "height": box_h,
                        "center_x": min_x + box_w // 2,
                        "center_y": min_y + box_h // 2,
                        "area": area,
                    }
                )

        candidates.sort(key=lambda item: (item["center_y"], item["center_x"]))
        deduped: list[dict] = []
        for candidate in candidates:
            if any(
                abs(candidate["center_y"] - item["center_y"]) < 16 and abs(candidate["center_x"] - item["center_x"]) < 16
                for item in deduped
            ):
                continue
            deduped.append(candidate)

        if len(deduped) > 4:
            clusters = []
            for candidate in deduped:
                cluster = [item for item in deduped if abs(item["center_x"] - candidate["center_x"]) <= 30]
                clusters.append(cluster)
            cluster = max(clusters, key=lambda group: (len(group), sum(item["area"] for item in group)))
            cluster.sort(key=lambda item: item["center_y"])
            if len(cluster) > 4:
                windows = [cluster[index : index + 4] for index in range(len(cluster) - 3)]
                cluster = max(windows, key=lambda group: sum(item["area"] for item in group))
            deduped = cluster
        return sorted(deduped, key=lambda item: item["center_y"])[:6]

    def parse_choice_options(self, image: Image.Image, region: Region | None = None) -> list[dict]:
        region = region or self.config.region
        markers = self.detect_choice_markers(image)
        if len(markers) < 2:
            return []
        markers = markers[:4]
        gaps = [markers[index + 1]["center_y"] - markers[index]["center_y"] for index in range(len(markers) - 1)]
        row_height = int(max(42, min(96, sorted(gaps)[len(gaps) // 2] if gaps else markers[0]["height"] * 2)))
        letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        origin_x, origin_y = virtual_screen_origin()
        options: list[dict] = []
        for index, marker in enumerate(markers):
            letter = letters[index]
            y1 = max(0, marker["center_y"] - row_height // 2)
            y2 = min(image.height, marker["center_y"] + row_height // 2)
            x1 = min(image.width, marker["left"] + marker["width"] + 8)
            x2 = image.width
            row_image = image.crop((x1, y1, x2, y2))
            option_text = self.ocr_short_text(row_image)
            option_text = option_text.replace("|", "").strip()
            options.append(
                {
                    "letter": letter,
                    "text": option_text,
                    "left": x1,
                    "top": y1,
                    "width": max(1, x2 - x1),
                    "height": max(1, y2 - y1),
                    "marker_left": marker["left"],
                    "marker_top": marker["top"],
                    "marker_width": marker["width"],
                    "marker_height": marker["height"],
                    "screen_center_x": origin_x + region.x + marker["center_x"],
                    "screen_center_y": origin_y + region.y + marker["center_y"],
                }
            )
        return options

    def format_choice_text(self, options: list[dict]) -> str:
        lines = []
        for option in options:
            text = option.get("text", "")
            lines.append(f"{option['letter']}. {text}".rstrip())
        return "\n".join(lines).strip()

    def choice_items(self, options: list[dict]) -> list[dict]:
        items: list[dict] = []
        for option in options:
            label = f"{option['letter']}. {option.get('text', '')}".rstrip()
            items.append(
                {
                    "index": 0,
                    "text": label,
                    "confidence": None,
                    "psm": "choice",
                    "left": option["left"],
                    "top": option["top"],
                    "width": option["width"],
                    "height": option["height"],
                    "screen_left": option["screen_center_x"],
                    "screen_top": option["screen_center_y"],
                    "screen_center_x": option["screen_center_x"],
                    "screen_center_y": option["screen_center_y"],
                }
            )
        return items

    def ocr_region_text(self, image: Image.Image, region: Region | None = None) -> str:
        base_text = self.ocr_image(image)
        if not self.config.choice_enhance:
            return base_text
        options = self.parse_choice_options(image, region)
        choice_text = self.format_choice_text(options)
        if not choice_text:
            return base_text
        question_text = ""
        first_marker_top = min((option["marker_top"] for option in options), default=0)
        if first_marker_top > 20:
            question_crop = image.crop((0, 0, image.width, max(1, first_marker_top - 6)))
            question_text = self.ocr_image(question_crop)
        question_text = question_text or base_text
        if question_text:
            return f"{question_text}\n\n选项：\n{choice_text}"
        return f"选项：\n{choice_text}"

    def ocr_region_details(self) -> dict:
        screenshot = self.screenshot()
        region = self.config.region
        crop = self.crop_region(screenshot, region)
        processed, scale = self.preprocess_ocr_image(crop)
        if scale != 1:
            processed = processed.resize(crop.size, Image.Resampling.NEAREST)
        items = self.ocr_items(crop, region)
        options = self.parse_choice_options(crop, region) if self.config.choice_enhance else []
        if options:
            items.extend(self.choice_items(options))
            for item_index, item in enumerate(items, start=1):
                item["index"] = item_index
        q_text = self.ocr_region_text(crop, region)
        return {
            "region": asdict(region),
            "image": processed,
            "original_image": crop,
            "items": items,
            "choices": options,
            "text": q_text,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "preprocess": self.preprocess_summary(),
        }

    def annotate_ocr_image(self, image: Image.Image, items: list[dict]) -> Image.Image:
        annotated = image.convert("RGB")
        draw = ImageDraw.Draw(annotated)
        for item in items:
            left = item["left"]
            top = item["top"]
            right = left + item["width"]
            bottom = top + item["height"]
            draw.rectangle((left, top, right, bottom), outline="#ff2d55", width=3)
            label = str(item["index"])
            label_box = draw.textbbox((0, 0), label)
            label_w = label_box[2] - label_box[0] + 8
            label_h = label_box[3] - label_box[1] + 6
            y = max(0, top - label_h)
            draw.rectangle((left, y, left + label_w, y + label_h), fill="#ff2d55")
            draw.text((left + 4, y + 3), label, fill="#ffffff")
        return annotated

    def find_text_position(self, text: str, region: Region | None = None) -> tuple[int, int] | None:
        """Return the center screen coordinate of the first OCR word containing text."""
        self.validate_ocr_setup()
        region = region or self.config.region
        image = self.crop_region(self.screenshot(), region)
        if self.config.choice_enhance:
            options = self.parse_choice_options(image, region)
            needle = text.strip().upper().rstrip(".")
            for option in options:
                option_text = option.get("text", "")
                if needle == option["letter"] or text.strip() in option_text:
                    return option["screen_center_x"], option["screen_center_y"]
        processed, scale = self.preprocess_ocr_image(image)
        psm = self.ocr_psm_list()[0]
        data = pytesseract.image_to_data(
            processed,
            lang=self.config.ocr_lang,
            config=self.tesseract_config(psm),
            output_type=pytesseract.Output.DICT,
        )
        needle = text.strip()
        if not needle:
            return None
        for index, value in enumerate(data.get("text", [])):
            if needle in value:
                origin_x, origin_y = virtual_screen_origin()
                x = origin_x + region.x + int(round(int(data["left"][index]) / scale)) + int(
                    round(int(data["width"][index]) / scale)
                ) // 2
                y = origin_y + region.y + int(round(int(data["top"][index]) / scale)) + int(
                    round(int(data["height"][index]) / scale)
                ) // 2
                return x, y
        return None

    def click_position(self, x: int, y: int) -> None:
        """Windows click helper kept out of the automatic loop."""
        ctypes.windll.user32.SetCursorPos(int(x), int(y))
        ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
        ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)

    def click_text(self, text: str, region: Region | None = None) -> tuple[int, int] | None:
        pos = self.find_text_position(text, region)
        if pos:
            self.click_position(*pos)
        return pos

    def api_url(self) -> str:
        base_url = self.config.api_base_url.strip().rstrip("/")
        if not base_url:
            raise RuntimeError("请求地址不能为空。")
        return f"{base_url}/models/{self.config.gemini_model}:generateContent"

    def api_key(self) -> str:
        api_key = self.config.api_key.strip() or os.getenv(self.config.api_key_env, "").strip()
        if not api_key:
            raise RuntimeError(f"没有 API Key，请设置环境变量 {self.config.api_key_env} 或在界面中填写。")
        return api_key

    def key_source(self) -> str:
        if self.config.api_key.strip():
            return "界面输入"
        if os.getenv(self.config.api_key_env, "").strip():
            return f"环境变量 {self.config.api_key_env} / .env"
        return "未设置"

    def post_generate_content(self, prompt: str, timeout: int = 90) -> dict:
        api_key = self.api_key()
        url = self.api_url()
        base_url = self.config.api_base_url.strip().rstrip("/")
        body = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": prompt,
                        }
                    ]
                }
            ]
        }
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        }
        if "generativelanguage.googleapis.com" not in base_url:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        started_at = time.perf_counter()
        result = {
            "ok": False,
            "url": url,
            "model": self.config.gemini_model,
            "key_source": self.key_source(),
            "http_status": None,
            "elapsed_ms": None,
            "payload": None,
            "raw": "",
            "error": "",
        }
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                result["ok"] = True
                result["http_status"] = response.status
                result["raw"] = raw
                result["payload"] = json.loads(raw)
        except urllib.error.HTTPError as exc:
            result["http_status"] = exc.code
            result["error"] = exc.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as exc:
            result["error"] = f"网络错误：{exc}"
        except json.JSONDecodeError as exc:
            result["error"] = f"返回内容不是合法 JSON：{exc}"
        finally:
            result["elapsed_ms"] = round((time.perf_counter() - started_at) * 1000)
        return result

    def extract_answer(self, payload: dict) -> str:
        parts = payload.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        return "".join(part.get("text", "") for part in parts).strip()

    def ask_gemini(self, q_text: str) -> str:
        prompt = f"{self.config.prompt_u.strip()}\n\nQ:\n{q_text.strip()}"
        result = self.post_generate_content(prompt)
        if not result["ok"]:
            detail = result["error"] or result["raw"] or "无错误详情"
            raise RuntimeError(f"API 请求失败：HTTP {result['http_status'] or '-'}\n{detail}")
        answer = self.extract_answer(result["payload"] or {})
        if not answer:
            raw = result["raw"] or json.dumps(result["payload"], ensure_ascii=False)
            raise RuntimeError(f"Gemini 没有返回文本：{raw[:800]}")
        return answer

    def test_ai_connectivity(self) -> dict:
        prompt = "连通性测试：请只回复 OK。"
        result = self.post_generate_content(prompt, timeout=30)
        if result["ok"]:
            result["answer"] = self.extract_answer(result["payload"] or {})
        return result

    def run_once(self) -> dict:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        screenshot = self.screenshot()
        crop = self.crop_region(screenshot)

        snapshot_path = ""
        if self.config.save_snapshots:
            SNAPSHOT_DIR.mkdir(exist_ok=True)
            filename = time.strftime("%Y%m%d_%H%M%S") + ".png"
            snapshot_path = str(SNAPSHOT_DIR / filename)
            crop.save(snapshot_path)

        q_text = self.ocr_region_text(crop, self.config.region)
        answer = self.ask_gemini(q_text) if q_text else "OCR 未识别到文字，已跳过 Gemini 请求。"
        record = {
            "time": timestamp,
            "region": asdict(self.config.region),
            "q": q_text,
            "answer": answer,
            "snapshot": snapshot_path,
        }
        with RESULTS_PATH.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record


class OcrVisualizerWindow:
    def __init__(self, root: Tk, details: dict, annotated: Image.Image):
        self.window = Toplevel(root)
        self.window.title("OCR 文字位置可视化")
        self.window.geometry("1180x760")
        self.window.minsize(900, 560)
        self.window._ocr_visualizer_ref = self
        self.details = details
        self.source_image = annotated
        self.photo = None
        self.build_ui()

    def build_ui(self) -> None:
        outer = ttk.Frame(self.window, padding=10)
        outer.pack(fill=BOTH, expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 8))
        region = self.details.get("region", {})
        source = "缓存命中" if self.details.get("cache_hit") else "新 OCR"
        ttk.Label(
            header,
            text=(
                f"时间：{self.details.get('time', '-')}    "
                f"区域：x={region.get('x')}, y={region.get('y')}, "
                f"w={region.get('w')}, h={region.get('h')}    "
                f"识别项：{len(self.details.get('items', []))}    "
                f"来源：{source}    "
                f"前处理：{self.details.get('preprocess', '-')}"
            ),
        ).pack(side=LEFT)

        pane = ttk.PanedWindow(outer, orient="horizontal")
        pane.pack(fill=BOTH, expand=True)

        image_frame = ttk.LabelFrame(pane, text="OCR 截图", padding=8)
        pane.add(image_frame, weight=3)

        canvas_frame = ttk.Frame(image_frame)
        canvas_frame.pack(fill=BOTH, expand=True)
        canvas = Canvas(canvas_frame, background="#f4f4f4", highlightthickness=0)
        x_scroll = ttk.Scrollbar(canvas_frame, orient="horizontal", command=canvas.xview)
        y_scroll = ttk.Scrollbar(canvas_frame, orient="vertical", command=canvas.yview)
        canvas.configure(xscrollcommand=x_scroll.set, yscrollcommand=y_scroll.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        canvas_frame.columnconfigure(0, weight=1)
        canvas_frame.rowconfigure(0, weight=1)

        display = self.source_image.copy()
        max_w, max_h = 1100, 680
        scale = min(max_w / display.width, max_h / display.height, 1.0)
        if scale < 1.0:
            display = display.resize((int(display.width * scale), int(display.height * scale)), Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(display)
        canvas.create_image(0, 0, image=self.photo, anchor="nw")
        canvas.image = self.photo
        canvas.configure(scrollregion=(0, 0, display.width, display.height))
        ttk.Label(image_frame, text=f"显示比例：{scale:.2f}，框号对应右侧表格").pack(anchor="w", pady=(6, 0))

        right = ttk.Frame(pane)
        pane.add(right, weight=2)

        table_box = ttk.LabelFrame(right, text="文字与坐标", padding=8)
        table_box.pack(fill=BOTH, expand=True)
        columns = ("idx", "text", "conf", "pos", "size", "center")
        tree = ttk.Treeview(table_box, columns=columns, show="headings", height=12)
        headers = {
            "idx": "#",
            "text": "文字",
            "conf": "置信度",
            "pos": "区域坐标",
            "size": "尺寸",
            "center": "屏幕中心",
        }
        widths = {"idx": 42, "text": 170, "conf": 64, "pos": 110, "size": 80, "center": 120}
        for key in columns:
            tree.heading(key, text=headers[key])
            tree.column(key, width=widths[key], anchor="w", stretch=(key == "text"))
        table_scroll = ttk.Scrollbar(table_box, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=table_scroll.set)
        tree.pack(side=LEFT, fill=BOTH, expand=True)
        table_scroll.pack(side=RIGHT, fill="y")
        tree.tag_configure("hit", background="#dcfce7")
        highlight_index = self.details.get("highlight_index")
        highlight_row = None
        for item in self.details.get("items", []):
            conf = "" if item.get("confidence") is None else item["confidence"]
            tags = ("hit",) if item.get("index") == highlight_index else ()
            row_id = tree.insert(
                "",
                END,
                values=(
                    item["index"],
                    item["text"],
                    conf,
                    f"{item['left']},{item['top']}",
                    f"{item['width']}x{item['height']}",
                    f"{item['screen_center_x']},{item['screen_center_y']}",
                ),
                tags=tags,
            )
            if tags:
                highlight_row = row_id
        if highlight_row:
            tree.selection_set(highlight_row)
            tree.see(highlight_row)

        text_box = ttk.LabelFrame(right, text="完整 OCR 文本 Q", padding=8)
        text_box.pack(fill=BOTH, expand=True, pady=(8, 0))
        text = Text(text_box, height=8, wrap="word")
        text.pack(fill=BOTH, expand=True)
        text.insert("1.0", self.details.get("text", ""))
        text.configure(state=DISABLED)


class RegionPicker:
    def __init__(self, root: Tk, image: Image.Image, on_pick):
        self.root = root
        self.image = image
        self.on_pick = on_pick
        self.origin_x, self.origin_y, self.screen_w, self.screen_h = virtual_screen_bounds()
        self.image_w, self.image_h = image.size
        self.scale_x = self.image_w / max(1, self.screen_w)
        self.scale_y = self.image_h / max(1, self.screen_h)
        display_image = image
        if (self.image_w, self.image_h) != (self.screen_w, self.screen_h):
            display_image = image.resize((self.screen_w, self.screen_h), Image.Resampling.BILINEAR)

        self.window = Toplevel(root)
        self.window.title("拖拽选择 OCR 区域")
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.geometry(f"{self.screen_w}x{self.screen_h}+0+0")
        self.window.bind("<Escape>", lambda _event: self.window.destroy())
        self.window.bind("<ButtonPress-3>", lambda _event: self.window.destroy())

        from tkinter import Canvas

        self.canvas = Canvas(
            self.window,
            width=self.screen_w,
            height=self.screen_h,
            cursor="crosshair",
            highlightthickness=0,
        )
        self.canvas.pack(fill=BOTH, expand=True)
        self.photo = ImageTk.PhotoImage(display_image)
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        self.canvas.create_rectangle(0, 0, self.screen_w, 34, fill="#111111", outline="")
        self.canvas.create_text(
            16,
            17,
            text="拖拽选择 OCR 区域，Esc 或右键取消",
            fill="#ffffff",
            anchor="w",
            font=("Microsoft YaHei UI", 11),
        )
        self.start_x = 0
        self.start_y = 0
        self.rect_id = None
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        move_window_absolute(self.window, self.origin_x, self.origin_y, self.screen_w, self.screen_h)
        self.window.lift()
        self.window.focus_force()
        self.window.grab_set()

    def clamp_point(self, x: int, y: int) -> tuple[int, int]:
        return (
            max(0, min(int(x), self.screen_w - 1)),
            max(0, min(int(y), self.screen_h - 1)),
        )

    def display_to_image_region(self, x1: int, y1: int, x2: int, y2: int) -> Region:
        left = int(round(x1 * self.scale_x))
        top = int(round(y1 * self.scale_y))
        right = int(round(x2 * self.scale_x))
        bottom = int(round(y2 * self.scale_y))
        left = max(0, min(left, self.image_w - 1))
        top = max(0, min(top, self.image_h - 1))
        right = max(left + 1, min(right, self.image_w))
        bottom = max(top + 1, min(bottom, self.image_h))
        return Region("target", left, top, right - left, bottom - top)

    def on_press(self, event):
        self.start_x, self.start_y = self.clamp_point(event.x, event.y)
        self.rect_id = self.canvas.create_rectangle(
            self.start_x,
            self.start_y,
            self.start_x,
            self.start_y,
            outline="#ff2d55",
            width=3,
        )

    def on_drag(self, event):
        if self.rect_id:
            x, y = self.clamp_point(event.x, event.y)
            self.canvas.coords(self.rect_id, self.start_x, self.start_y, x, y)

    def on_release(self, event):
        end_x, end_y = self.clamp_point(event.x, event.y)
        x1, x2 = sorted((self.start_x, end_x))
        y1, y2 = sorted((self.start_y, end_y))
        region = self.display_to_image_region(x1, y1, x2, y2)
        if region.w >= 5 and region.h >= 5:
            self.on_pick(region)
        self.window.destroy()


class App:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title("屏幕 OCR + Gemini")
        self.config = load_config()
        self.worker = ScreenGeminiWorker(self.config)
        self.running = False
        self.busy = False
        self.preview_image = None
        self.preview_photo = None
        self.region_corner_points: dict[str, tuple[int, int]] = {}

        self.interval_var = StringVar(value=str(self.config.interval_seconds))
        self.base_url_var = StringVar(value=self.config.api_base_url)
        self.model_var = StringVar(value=self.config.gemini_model)
        self.api_key_var = StringVar(value=self.config.api_key)
        self.tesseract_var = StringVar(value=self.config.tesseract_cmd)
        self.lang_var = StringVar(value=self.config.ocr_lang)
        self.preprocess_grayscale_var = BooleanVar(value=self.config.preprocess_grayscale)
        self.preprocess_autocontrast_var = BooleanVar(value=self.config.preprocess_autocontrast)
        self.preprocess_binarize_var = BooleanVar(value=self.config.preprocess_binarize)
        self.binary_threshold_var = StringVar(value=str(self.config.binary_threshold))
        self.preprocess_invert_var = BooleanVar(value=self.config.preprocess_invert)
        self.preprocess_scale_var = StringVar(value=str(self.config.preprocess_scale))
        self.preprocess_sharpen_var = BooleanVar(value=self.config.preprocess_sharpen)
        self.ocr_psm_modes_var = StringVar(value=self.config.ocr_psm_modes)
        self.ocr_multi_pass_var = BooleanVar(value=self.config.ocr_multi_pass)
        self.choice_enhance_var = BooleanVar(value=self.config.choice_enhance)
        self.region_vars = {
            "x": StringVar(value=str(self.config.region.x)),
            "y": StringVar(value=str(self.config.region.y)),
            "w": StringVar(value=str(self.config.region.w)),
            "h": StringVar(value=str(self.config.region.h)),
        }
        self.status_var = StringVar(value=self.engine_status())
        self.find_text_var = StringVar()
        self.position_var = StringVar(value="未定位")

        self.build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def engine_status(self) -> str:
        if pytesseract is None:
            return "OCR 未就绪：缺少 pytesseract"
        if not self.config.tesseract_cmd:
            return "OCR 未就绪：未找到 tesseract.exe"
        langs = tesseract_languages(self.config.tesseract_cmd)
        missing = missing_ocr_languages(self.config.tesseract_cmd, self.config.ocr_lang)
        if missing:
            installed = "+".join(langs) if langs else "无"
            return f"OCR 语言缺失：{'+'.join(missing)}；已安装：{installed}"
        return f"OCR 就绪：{Path(self.config.tesseract_cmd).name} / {self.config.ocr_lang}"

    def build_ui(self) -> None:
        root_frame = ttk.Frame(self.root, padding=10)
        root_frame.pack(fill=BOTH, expand=True)

        toolbar = ttk.Frame(root_frame)
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Button(toolbar, text="保存设置", command=self.save_from_ui).pack(side=LEFT)
        ttk.Button(toolbar, text="测试 AI 连通性", command=self.test_ai_async).pack(side=LEFT, padx=(8, 0))
        ttk.Button(toolbar, text="立即执行一次", command=self.run_once_async).pack(side=LEFT, padx=(8, 0))
        ttk.Button(toolbar, text="OCR 可视化", command=self.visualize_ocr_async).pack(side=LEFT, padx=(8, 0))
        self.start_button = ttk.Button(toolbar, text="开始循环", command=self.toggle_loop)
        self.start_button.pack(side=LEFT, padx=(8, 0))
        ttk.Label(toolbar, textvariable=self.status_var).pack(side=RIGHT)

        content = ttk.PanedWindow(root_frame, orient="horizontal")
        content.pack(fill=BOTH, expand=True)

        preview_box = ttk.LabelFrame(content, text="目标区域预览", padding=8)
        content.add(preview_box, weight=3)
        self.preview_label = ttk.Label(preview_box, text="点击“截图预览”或“OCR 可视化”查看目标区域", anchor="center")
        self.preview_label.pack(fill=BOTH, expand=True)

        controls = ttk.Notebook(content)
        content.add(controls, weight=1)

        run_tab = ttk.Frame(controls, padding=10)
        region_tab = ttk.Frame(controls, padding=10)
        tool_tab = ttk.Frame(controls, padding=10)
        controls.add(run_tab, text="运行")
        controls.add(region_tab, text="区域")
        controls.add(tool_tab, text="工具")

        self.add_entry(run_tab, "间隔秒数", self.interval_var)
        self.add_entry(run_tab, "请求地址", self.base_url_var)
        self.add_entry(run_tab, "Gemini 模型", self.model_var)
        self.add_entry(run_tab, "API Key", self.api_key_var, show="*")
        prompt_box = ttk.LabelFrame(run_tab, text="提示语 U", padding=8)
        prompt_box.pack(fill=BOTH, expand=True, pady=(8, 0))
        self.prompt_input = Text(prompt_box, height=9, wrap="word")
        self.prompt_input.insert("1.0", self.config.prompt_u)
        self.prompt_input.pack(fill=BOTH, expand=True)

        coord_box = ttk.LabelFrame(region_tab, text="目标区域坐标", padding=8)
        coord_box.pack(fill="x")
        for col, key in enumerate(("x", "y", "w", "h")):
            ttk.Label(coord_box, text=key.upper()).grid(row=0, column=col, padx=4, sticky="w")
            ttk.Entry(coord_box, textvariable=self.region_vars[key], width=9).grid(row=1, column=col, padx=4, sticky="ew")
            coord_box.columnconfigure(col, weight=1)
        ttk.Button(region_tab, text="拖拽选择区域", command=self.pick_region).pack(fill="x", pady=(10, 0))
        corner_buttons = ttk.Frame(region_tab)
        corner_buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(corner_buttons, text="3秒后取左上角", command=lambda: self.capture_region_corner_after_delay("tl")).pack(
            side=LEFT,
            fill="x",
            expand=True,
        )
        ttk.Button(corner_buttons, text="3秒后取右下角", command=lambda: self.capture_region_corner_after_delay("br")).pack(
            side=RIGHT,
            fill="x",
            expand=True,
            padx=(8, 0),
        )
        ttk.Button(region_tab, text="截图预览", command=self.preview_region).pack(fill="x", pady=(8, 0))
        ttk.Button(region_tab, text="OCR 可视化窗口", command=self.visualize_ocr_async).pack(fill="x", pady=(8, 0))

        self.add_entry(tool_tab, "OCR 语言", self.lang_var)
        ttk.Label(tool_tab, text="Tesseract").pack(anchor="w", pady=(8, 0))
        ttk.Entry(tool_tab, textvariable=self.tesseract_var).pack(fill="x")
        tess_buttons = ttk.Frame(tool_tab)
        tess_buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(tess_buttons, text="自动检测 Tesseract", command=self.auto_detect_tesseract).pack(side=LEFT, fill="x", expand=True)
        ttk.Button(tess_buttons, text="选择", command=self.pick_tesseract).pack(side=RIGHT, padx=(8, 0))

        preprocess_box = ttk.LabelFrame(tool_tab, text="OCR 前处理", padding=8)
        preprocess_box.pack(fill="x", pady=(12, 0))
        ttk.Checkbutton(preprocess_box, text="灰度", variable=self.preprocess_grayscale_var).pack(anchor="w")
        ttk.Checkbutton(preprocess_box, text="自动对比度", variable=self.preprocess_autocontrast_var).pack(anchor="w")
        ttk.Checkbutton(preprocess_box, text="二值化", variable=self.preprocess_binarize_var).pack(anchor="w")
        ttk.Checkbutton(preprocess_box, text="反色", variable=self.preprocess_invert_var).pack(anchor="w")
        ttk.Checkbutton(preprocess_box, text="锐化", variable=self.preprocess_sharpen_var).pack(anchor="w")
        ttk.Checkbutton(preprocess_box, text="选择题增强解析", variable=self.choice_enhance_var).pack(anchor="w")
        threshold_row = ttk.Frame(preprocess_box)
        threshold_row.pack(fill="x", pady=(8, 0))
        ttk.Label(threshold_row, text="二值化阈值").pack(side=LEFT)
        ttk.Spinbox(threshold_row, from_=0, to=255, increment=5, textvariable=self.binary_threshold_var, width=8).pack(
            side=RIGHT
        )
        scale_row = ttk.Frame(preprocess_box)
        scale_row.pack(fill="x", pady=(8, 0))
        ttk.Label(scale_row, text="OCR 放大倍数").pack(side=LEFT)
        ttk.Spinbox(scale_row, from_=1, to=4, increment=1, textvariable=self.preprocess_scale_var, width=8).pack(side=RIGHT)
        psm_row = ttk.Frame(preprocess_box)
        psm_row.pack(fill="x", pady=(8, 0))
        ttk.Label(psm_row, text="PSM 模式").pack(side=LEFT)
        ttk.Entry(psm_row, textvariable=self.ocr_psm_modes_var, width=12).pack(side=RIGHT)
        ttk.Checkbutton(preprocess_box, text="多 PSM 模式合并", variable=self.ocr_multi_pass_var).pack(anchor="w", pady=(6, 0))
        ttk.Label(preprocess_box, text="常用范围：150-210；深色背景白字可试试反色。").pack(anchor="w", pady=(6, 0))

        locate_box = ttk.LabelFrame(tool_tab, text="文字定位/点击辅助", padding=8)
        locate_box.pack(fill="x", pady=(12, 0))
        ttk.Entry(locate_box, textvariable=self.find_text_var).pack(fill="x")
        locate_buttons = ttk.Frame(locate_box)
        locate_buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(locate_buttons, text="定位", command=self.locate_text_async).pack(side=LEFT, fill="x", expand=True)
        ttk.Button(locate_buttons, text="点击", command=self.click_text_async).pack(side=RIGHT, fill="x", expand=True, padx=(8, 0))
        ttk.Label(locate_box, textvariable=self.position_var).pack(anchor="w", pady=(8, 0))

        result_box = ttk.LabelFrame(root_frame, text="运行结果", padding=8)
        result_box.pack(fill=BOTH, expand=False, pady=(8, 0))
        output_frame = ttk.Frame(result_box)
        output_frame.pack(fill=BOTH, expand=True)
        self.output = Text(output_frame, height=10, wrap="word")
        output_scroll = ttk.Scrollbar(output_frame, orient="vertical", command=self.output.yview)
        self.output.configure(yscrollcommand=output_scroll.set)
        self.output.pack(side=LEFT, fill=BOTH, expand=True)
        output_scroll.pack(side=RIGHT, fill="y")

    def add_entry(self, parent, label: str, variable: StringVar, show: str | None = None) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text=label).pack(anchor="w")
        ttk.Entry(row, textvariable=variable, show=show).pack(fill="x")

    def pick_tesseract(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 tesseract.exe",
            filetypes=[("tesseract.exe", "tesseract.exe"), ("Executable", "*.exe"), ("All files", "*.*")],
        )
        if path:
            self.tesseract_var.set(path)
            self.status_var.set(f"已选择 Tesseract：{path}")

    def auto_detect_tesseract(self) -> None:
        path = detect_tesseract_cmd()
        if path:
            self.tesseract_var.set(path)
            self.save_from_ui()
            self.append_output(f"已自动检测到 Tesseract：{path}\n{'-' * 48}")
        else:
            message = "没有自动检测到 tesseract.exe，请点击“选择”手动指定安装目录里的 tesseract.exe。"
            self.status_var.set(message)
            self.append_output(message + f"\n{'-' * 48}")

    def read_region_from_ui(self) -> Region:
        return Region(
            "target",
            int(self.region_vars["x"].get()),
            int(self.region_vars["y"].get()),
            int(self.region_vars["w"].get()),
            int(self.region_vars["h"].get()),
        )

    def update_region_ui(self, region: Region) -> None:
        self.region_vars["x"].set(str(region.x))
        self.region_vars["y"].set(str(region.y))
        self.region_vars["w"].set(str(region.w))
        self.region_vars["h"].set(str(region.h))

    def save_from_ui(self) -> bool:
        try:
            self.config.interval_seconds = max(1, int(self.interval_var.get()))
            self.config.region = self.read_region_from_ui()
            self.config.prompt_u = self.prompt_input.get("1.0", END).strip()
            self.config.api_base_url = self.base_url_var.get().strip() or "http://192.168.31.114:7999/v1beta"
            self.config.gemini_model = self.model_var.get().strip() or "gemini-3-pro-preview"
            self.config.api_key = self.api_key_var.get().strip()
            self.config.tesseract_cmd = self.tesseract_var.get().strip()
            self.config.ocr_lang = self.lang_var.get().strip() or "chi_sim+eng"
            self.config.preprocess_grayscale = bool(self.preprocess_grayscale_var.get())
            self.config.preprocess_autocontrast = bool(self.preprocess_autocontrast_var.get())
            self.config.preprocess_binarize = bool(self.preprocess_binarize_var.get())
            self.config.binary_threshold = max(0, min(255, int(self.binary_threshold_var.get())))
            self.binary_threshold_var.set(str(self.config.binary_threshold))
            self.config.preprocess_invert = bool(self.preprocess_invert_var.get())
            self.config.preprocess_scale = max(1, min(4, int(self.preprocess_scale_var.get())))
            self.preprocess_scale_var.set(str(self.config.preprocess_scale))
            self.config.preprocess_sharpen = bool(self.preprocess_sharpen_var.get())
            self.config.ocr_psm_modes = self.ocr_psm_modes_var.get().strip() or "6,11"
            self.config.ocr_multi_pass = bool(self.ocr_multi_pass_var.get())
            self.config.choice_enhance = bool(self.choice_enhance_var.get())
            save_config(self.config)
            self.worker = ScreenGeminiWorker(self.config)
            self.status_var.set(f"设置已保存；{self.engine_status()}")
            return True
        except Exception as exc:
            messagebox.showerror("设置错误", str(exc))
            return False

    def pick_region(self) -> None:
        self.save_from_ui()
        image = self.worker.screenshot()
        origin_x, origin_y, screen_w, screen_h = virtual_screen_bounds()
        self.append_output(
            "区域选择器已打开\n"
            f"虚拟桌面: origin=({origin_x}, {origin_y}), size={screen_w}x{screen_h}\n"
            f"截图尺寸: {image.size[0]}x{image.size[1]}\n"
            f"{'-' * 48}"
        )
        RegionPicker(self.root, image, self.on_region_picked)

    def on_region_picked(self, region: Region) -> None:
        self.update_region_ui(region)
        self.save_from_ui()
        origin_x, origin_y = virtual_screen_origin()
        self.append_output(
            "已选择 OCR 区域\n"
            f"截图坐标: x={region.x}, y={region.y}, w={region.w}, h={region.h}\n"
            f"真实屏幕左上角: ({origin_x + region.x}, {origin_y + region.y})\n"
            f"真实屏幕右下角: ({origin_x + region.x + region.w}, {origin_y + region.y + region.h})\n"
            f"{'-' * 48}"
        )
        self.preview_region()

    def capture_region_corner_after_delay(self, corner: str) -> None:
        label = "左上角" if corner == "tl" else "右下角"
        self.append_output(f"请在 3 秒内把鼠标移动到目标区域{label}...\n{'-' * 48}")
        self.status_var.set(f"等待记录{label}...")
        self.root.iconify()
        self.root.after(3000, lambda: self.capture_region_corner(corner))

    def capture_region_corner(self, corner: str) -> None:
        screen_x, screen_y = cursor_position()
        image = self.worker.screenshot()
        image_x, image_y = self.worker.screen_point_to_image_point(screen_x, screen_y, image)
        self.region_corner_points[corner] = (image_x, image_y)
        self.root.deiconify()
        self.root.lift()
        label = "左上角" if corner == "tl" else "右下角"
        self.append_output(
            f"已记录{label}\n"
            f"真实屏幕坐标: ({screen_x}, {screen_y})\n"
            f"截图坐标: ({image_x}, {image_y})\n"
            f"{'-' * 48}"
        )
        if "tl" in self.region_corner_points and "br" in self.region_corner_points:
            x1, y1 = self.region_corner_points["tl"]
            x2, y2 = self.region_corner_points["br"]
            left, right = sorted((x1, x2))
            top, bottom = sorted((y1, y2))
            region = Region("target", left, top, max(1, right - left), max(1, bottom - top))
            self.on_region_picked(region)
            self.region_corner_points.clear()
        else:
            self.status_var.set(f"已记录{label}，请继续记录另一个角点")

    def preview_region(self) -> None:
        if not self.save_from_ui():
            return
        try:
            image = self.worker.crop_region(self.worker.screenshot())
            image.thumbnail((900, 560))
            self.preview_image = image
            self.preview_photo = ImageTk.PhotoImage(image)
            self.preview_label.configure(image=self.preview_photo, text="")
        except Exception as exc:
            messagebox.showerror("截图失败", str(exc))

    def update_preview_image(self, image: Image.Image) -> None:
        display = image.copy()
        display.thumbnail((940, 620))
        self.preview_image = display
        self.preview_photo = ImageTk.PhotoImage(display)
        self.preview_label.configure(image=self.preview_photo, text="")

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = DISABLED if busy else NORMAL
        self.start_button.configure(state=NORMAL)
        self.status_var.set("运行中..." if busy else ("循环中" if self.running else "就绪"))

    def append_output(self, text: str) -> None:
        self.output.insert(END, text + "\n")
        self.output.see(END)

    def redact_secret(self, text: str) -> str:
        redacted = text
        keys = {self.config.api_key.strip(), os.getenv(self.config.api_key_env, "").strip()}
        for key in keys:
            if key:
                redacted = redacted.replace(key, "<redacted>")
        return redacted

    def format_ai_test_result(self, result: dict) -> str:
        status = "成功" if result.get("ok") else "失败"
        lines = [
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] AI 连通性测试：{status}",
            f"URL: {result.get('url', '-')}",
            f"Model: {result.get('model', '-')}",
            f"Key: {result.get('key_source', '-')}",
            f"HTTP: {result.get('http_status') or '-'}",
            f"耗时: {result.get('elapsed_ms') or '-'} ms",
        ]
        if result.get("ok"):
            lines.append(f"返回: {result.get('answer') or '(空返回)'}")
        else:
            detail = result.get("error") or result.get("raw") or "无错误详情"
            lines.extend(["错误详情:", self.redact_secret(str(detail))[:1600]])
        lines.append("-" * 48)
        return "\n".join(lines)

    def run_once_async(self) -> None:
        if self.busy or not self.save_from_ui():
            return
        self.set_busy(True)
        thread = threading.Thread(target=self._run_once_thread, daemon=True)
        thread.start()

    def visualize_ocr_async(self) -> None:
        if self.busy or not self.save_from_ui():
            return
        self.set_busy(True)
        threading.Thread(target=self._visualize_ocr_thread, daemon=True).start()

    def _visualize_ocr_thread(self) -> None:
        try:
            details = self.worker.ocr_region_details()
            annotated = self.worker.annotate_ocr_image(details["image"], details["items"])

            def show_window():
                self.update_preview_image(annotated)
                OcrVisualizerWindow(self.root, details, annotated)
                self.append_output(
                    f"[{details['time']}] OCR 可视化完成：识别到 {len(details['items'])} 个文字块\n"
                    f"前处理：{details.get('preprocess', '-')}\n"
                    f"{'-' * 48}"
                )

            self.root.after(0, show_window)
        except Exception as exc:
            self.root.after(0, lambda: messagebox.showerror("OCR 可视化失败", str(exc)))
            self.root.after(0, lambda: self.append_output(f"OCR 可视化失败：{exc}\n{'-' * 48}"))
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
            self.root.after(0, lambda: self.append_output(self.format_ai_test_result(result)))
        except Exception as exc:
            detail = self.redact_secret(str(exc))
            message = (
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] AI 连通性测试：失败\n"
                f"错误详情:\n{detail}\n"
                f"{'-' * 48}"
            )
            self.root.after(0, lambda: self.append_output(message))
            self.root.after(0, lambda: messagebox.showerror("AI 连通性测试失败", detail))
        finally:
            self.root.after(0, lambda: self.set_busy(False))

    def _run_once_thread(self) -> None:
        try:
            record = self.worker.run_once()
            message = (
                f"[{record['time']}]\n"
                f"Q/OCR:\n{record['q'] or '(空)'}\n\n"
                f"Gemini:\n{record['answer']}\n"
                f"{'-' * 48}"
            )
            self.root.after(0, lambda: self.append_output(message))
        except Exception as exc:
            self.root.after(0, lambda: messagebox.showerror("执行失败", str(exc)))
            self.root.after(0, lambda: self.append_output(f"执行失败：{exc}\n{'-' * 48}"))
        finally:
            self.root.after(0, lambda: self.set_busy(False))
            if self.running:
                self.root.after(self.config.interval_seconds * 1000, self.run_once_async)

    def toggle_loop(self) -> None:
        if self.running:
            self.running = False
            self.start_button.configure(text="开始循环")
            self.status_var.set("已停止")
            return
        if not self.save_from_ui():
            return
        self.running = True
        self.start_button.configure(text="停止循环")
        self.run_once_async()

    def locate_text_async(self) -> None:
        self._text_action_async(click=False)

    def click_text_async(self) -> None:
        self._text_action_async(click=True)

    def _text_action_async(self, click: bool) -> None:
        text = self.find_text_var.get().strip()
        if not text:
            messagebox.showinfo("缺少文字", "请输入要定位的文字。")
            return
        if not self.save_from_ui():
            return

        def work():
            try:
                pos = self.worker.click_text(text) if click else self.worker.find_text_position(text)
                if pos:
                    msg = f"{'已点击' if click else '已定位'}：{text} -> {pos[0]}, {pos[1]}"
                else:
                    msg = f"未找到：{text}"
                self.root.after(0, lambda: self.position_var.set(msg))
                self.root.after(0, lambda: self.append_output(msg))
            except Exception as exc:
                self.root.after(0, lambda: messagebox.showerror("定位失败", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def on_close(self) -> None:
        self.running = False
        self.save_from_ui()
        self.root.destroy()


def main() -> None:
    enable_dpi_awareness()
    root = Tk()
    root.geometry("1360x900")
    root.minsize(980, 680)
    app = App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

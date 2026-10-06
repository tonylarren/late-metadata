import io
import logging
import threading

import cv2
import fitz  # PyMuPDF
import numpy as np
from PIL import Image, ImageSequence
from paddleocr import PaddleOCR

IMAGE_MIMES = {
    "image/png",
    "image/jpeg",
    "image/tiff",
    "image/bmp",
    "image/webp",
    "image/gif",
}
SUPPORTED_MIMES = IMAGE_MIMES | {"application/pdf"}

log = logging.getLogger("cv-ocr")


def build_engine(lang: str, device: str = "cpu", enable_mkldnn: bool = True) -> PaddleOCR:
    # Document orientation/unwarping models are overkill for CVs; text-line
    # orientation catches rotated snippets cheaply.
    return PaddleOCR(
        lang=lang,
        device=device,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=True,
        enable_mkldnn=enable_mkldnn,
    )


def gpu_available() -> bool:
    try:
        import paddle

        return paddle.device.is_compiled_with_cuda() and paddle.device.cuda.device_count() > 0
    except Exception:
        return False


def resolve_device(requested: str) -> str:
    """Map OCR_DEVICE (auto | cpu | gpu | gpu:N) to a device that actually exists."""
    requested = requested.strip().lower()
    if requested == "cpu":
        return "cpu"
    if gpu_available():
        return "gpu:0" if requested in ("auto", "gpu") else requested
    if requested != "auto":
        log.warning("OCR_DEVICE=%s but no usable GPU found; falling back to CPU", requested)
    return "cpu"


class OCRService:
    def __init__(
        self,
        lang: str,
        pdf_dpi: int,
        max_pages: int,
        enable_mkldnn: bool = True,
        device: str = "auto",
    ):
        self.device = resolve_device(device)
        try:
            self._engine = build_engine(lang, self.device, enable_mkldnn)
        except Exception:
            if self.device == "cpu":
                raise
            log.exception("Could not start PaddleOCR on %s; falling back to CPU", self.device)
            self.device = "cpu"
            self._engine = build_engine(lang, "cpu", enable_mkldnn)
        log.info("PaddleOCR running on %s", self.device)
        # PaddleOCR predictors are not thread-safe; serialize inference.
        self._lock = threading.Lock()
        self.pdf_dpi = pdf_dpi
        self.max_pages = max_pages

    def process(self, data: bytes, mime_type: str) -> list[dict]:
        """OCR a PDF or image. Returns one {"page", "text"} entry per page."""
        if mime_type == "application/pdf":
            images = self._pdf_to_images(data)
        elif mime_type in IMAGE_MIMES:
            images = self._decode_image(data)
        else:
            raise ValueError(f"Unsupported file type: {mime_type}")

        return [
            {"page": number, "text": "\n".join(self._ocr_image(img))}
            for number, img in enumerate(images, start=1)
        ]

    def _pdf_to_images(self, data: bytes) -> list[np.ndarray]:
        images = []
        with fitz.open(stream=data, filetype="pdf") as doc:
            for page in list(doc)[: self.max_pages]:
                pix = page.get_pixmap(dpi=self.pdf_dpi, alpha=False)
                rgb = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
                images.append(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
        return images

    def _decode_image(self, data: bytes) -> list[np.ndarray]:
        # PIL handles multi-page TIFFs and formats OpenCV can't decode.
        with Image.open(io.BytesIO(data)) as im:
            frames = [
                cv2.cvtColor(np.array(frame.convert("RGB")), cv2.COLOR_RGB2BGR)
                for frame in ImageSequence.Iterator(im)
            ]
        return frames[: self.max_pages]

    def _ocr_image(self, img: np.ndarray) -> list[str]:
        """Text lines of one page, in PaddleOCR's reading order."""
        with self._lock:
            results = self._engine.predict(img)
        return [text for res in results for text in res["rec_texts"] if text.strip()]

"""
PERF: optional helper to compress/resize uploaded images (driver photos,
payment proof screenshots) after they're saved to disk.

Design goals:
- Never breaks the upload flow: any failure (missing Pillow, corrupt
  image, unsupported format) is swallowed and the original saved file
  is left exactly as it was.
- Same filename/path in, same filename/path out - callers and templates
  that reference the stored path don't need any changes.
- Only touches actual raster image extensions; PDFs and anything else
  are left alone.
"""

import os

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
_MAX_DIMENSION = 1280   # plenty for a driver photo / payment screenshot
_JPEG_QUALITY = 80


def compress_image_file(path, max_dimension=_MAX_DIMENSION, quality=_JPEG_QUALITY):
    """
    Resize (if larger than max_dimension) and re-save an image in place
    to cut its file size, without changing its path/extension.
    Safe no-op on failure.
    """
    try:
        ext = os.path.splitext(path)[1].lower()
        if ext not in _IMAGE_EXTS or not os.path.isfile(path):
            return

        from PIL import Image

        with Image.open(path) as img:
            img_format = img.format  # keep original format (PNG stays PNG, etc.)

            if img.mode in ("RGBA", "P") and ext in (".jpg", ".jpeg"):
                img = img.convert("RGB")

            width, height = img.size
            if max(width, height) > max_dimension:
                scale = max_dimension / float(max(width, height))
                img = img.resize(
                    (max(1, int(width * scale)), max(1, int(height * scale))),
                    Image.LANCZOS
                )

            save_kwargs = {"optimize": True}
            if img_format == "JPEG":
                save_kwargs["quality"] = quality
            img.save(path, format=img_format, **save_kwargs)
    except Exception as e:
        # Non-fatal by design - original upload stays as-is.
        print("Image compression skipped (non-fatal):", e)

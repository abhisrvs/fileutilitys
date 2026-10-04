"""Worker functions for background conversion/compression jobs.

Each `run_<kind>` function is a thin shim around the original route logic,
but operating on staged files in `in_dir` rather than on `request.files`.
It writes its output to `out_dir` and returns (result_path, result_filename,
mimetype, size_bytes).

The dispatch table is in jobs._run_job — adding a new kind means:
  1. write a `run_<kind>` here,
  2. call enqueue_job(kind='<kind>', ...) from the route.
"""
import os
import zipfile
import logging

log = logging.getLogger(__name__)

ALLOWED_IMG = {"jpg", "jpeg", "png", "webp"}
ALLOWED_PDF = {"pdf"}
ALLOWED_DOC = {"docx"}

CONVERT_INPUT_EXT = {"jpg", "jpeg", "png", "webp", "bmp", "gif", "tif", "tiff", "ico"}
# target -> (PIL format, mimetype, uses_quality)
IMAGE_CONVERT_TARGETS = {
    "jpg": ("JPEG", "image/jpeg", True),
    "png": ("PNG", "image/png", False),
    "webp": ("WEBP", "image/webp", True),
    "bmp": ("BMP", "image/bmp", False),
    "tiff": ("TIFF", "image/tiff", False),
}


# ---------- helpers ----------

def _flatten_alpha(img):
    from PIL import Image
    rgba = img.convert("RGBA")
    bg = Image.new("RGB", rgba.size, (255, 255, 255))
    bg.paste(rgba, mask=rgba.split()[-1])
    return bg


def prepare_image_for_format(img, pil_fmt):
    has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    if pil_fmt == "JPEG":
        if has_alpha:
            return _flatten_alpha(img)
        return img.convert("RGB") if img.mode != "RGB" else img
    if pil_fmt in ("PNG", "WEBP", "TIFF"):
        if img.mode == "P":
            return img.convert("RGBA" if has_alpha else "RGB")
        if img.mode == "LA":
            return img.convert("RGBA")
        if img.mode == "CMYK":
            return img.convert("RGB")
        return img
    if pil_fmt == "BMP":
        if has_alpha:
            return _flatten_alpha(img)
        if img.mode not in ("RGB", "L"):
            return img.convert("RGB")
        return img
    return img.convert("RGB")


def _list_inputs(in_dir, allowed_exts):
    out = []
    for name in sorted(os.listdir(in_dir)):
        if '.' not in name:
            continue
        ext = name.rsplit('.', 1)[1].lower()
        if ext in allowed_exts:
            out.append((name, os.path.join(in_dir, name)))
    return out


def _stage_to_out_path(out_dir, dest_name):
    return os.path.join(out_dir, dest_name)


def _zip_outputs(out_dir, items, zip_name):
    zip_path = os.path.join(out_dir, zip_name)
    with zipfile.ZipFile(zip_path, 'w') as z:
        for path, arcname in items:
            z.write(path, arcname=arcname)
            try:
                os.remove(path)
            except Exception:
                pass
    return zip_path, zip_name, os.path.getsize(zip_path)


# ---------- workers ----------

def run_image_compress(params, in_dir, out_dir, progress):
    from PIL import Image, ImageOps

    files = _list_inputs(in_dir, ALLOWED_IMG)
    if not files:
        raise ValueError('No valid images provided')

    quality = int(params.get('quality', 75))
    outputs = []
    total = len(files)
    for i, (name, path) in enumerate(files, 1):
        img = Image.open(path)
        img = ImageOps.exif_transpose(img)
        img = prepare_image_for_format(img, "JPEG")
        base = name.rsplit('.', 1)[0]
        out_name = f"{base}-compressed.jpg"
        out_path = _stage_to_out_path(out_dir, out_name)
        img.save(out_path, "JPEG", quality=max(1, min(100, quality)), optimize=True)
        outputs.append((out_path, out_name))
        if progress and total:
            progress((i / total) * 0.95)

    if len(outputs) == 1:
        p, n = outputs[0]
        return p, n, 'image/jpeg', os.path.getsize(p)
    zip_path, zip_name, size = _zip_outputs(out_dir, outputs, 'images-compressed.zip')
    return zip_path, zip_name, 'application/zip', size


def run_image_convert(params, in_dir, out_dir, progress):
    from PIL import Image, ImageOps

    target = (params.get('format') or '').lower()
    if target not in IMAGE_CONVERT_TARGETS:
        raise ValueError('Unsupported target format')
    pil_fmt, mime, uses_quality = IMAGE_CONVERT_TARGETS[target]
    quality = int(params.get('quality', 90))

    files = _list_inputs(in_dir, CONVERT_INPUT_EXT)
    if not files:
        raise ValueError('No valid images provided')

    outputs = []
    total = len(files)
    for i, (name, path) in enumerate(files, 1):
        try:
            img = Image.open(path)
            img = ImageOps.exif_transpose(img)
        except Exception:
            log.exception('image convert: failed to open %s', name)
            continue
        img = prepare_image_for_format(img, pil_fmt)
        base = name.rsplit('.', 1)[0]
        out_name = f"{base}.{target}"
        out_path = _stage_to_out_path(out_dir, out_name)
        try:
            if uses_quality:
                img.save(out_path, pil_fmt, quality=quality, optimize=(pil_fmt == "JPEG"))
            else:
                img.save(out_path, pil_fmt)
        except Exception:
            log.exception('image convert: failed to save %s', name)
            try:
                os.remove(out_path)
            except Exception:
                pass
            continue
        outputs.append((out_path, out_name))
        if progress and total:
            progress((i / total) * 0.95)

    if not outputs:
        raise ValueError('No valid images converted')
    if len(outputs) == 1:
        p, n = outputs[0]
        return p, n, mime, os.path.getsize(p)
    zip_path, zip_name, size = _zip_outputs(out_dir, outputs, f'images-converted-{target}.zip')
    return zip_path, zip_name, 'application/zip', size


def run_pdf_compress(params, in_dir, out_dir, progress):
    from PyPDF2 import PdfReader, PdfWriter

    quality_choice = (params.get('quality') or 'medium').lower()

    try:
        import fitz as _fitz
    except Exception:
        _fitz = None

    files = _list_inputs(in_dir, ALLOWED_PDF)
    if not files:
        raise ValueError('No valid PDF uploaded')

    outputs = []
    total = len(files)
    for i, (name, path) in enumerate(files, 1):
        out_name = f"{name.rsplit('.', 1)[0]}-compressed.pdf"
        out_path = _stage_to_out_path(out_dir, out_name)
        try:
            if _fitz is not None:
                doc = _fitz.open(path)
                doc.save(out_path, garbage=4, deflate=True, clean=True)
                doc.close()
            else:
                reader = PdfReader(path)
                writer = PdfWriter()
                for page in reader.pages:
                    try:
                        page.compress_content_streams()
                    except Exception:
                        pass
                    writer.add_page(page)
                with open(out_path, 'wb') as f:
                    writer.write(f)
        except Exception as e:
            log.exception('pdf compress: failed on %s', name)
            try:
                os.remove(out_path)
            except Exception:
                pass
            continue
        outputs.append((out_path, out_name))
        if progress and total:
            progress((i / total) * 0.95)

    if not outputs:
        raise ValueError('No valid PDFs processed')
    if len(outputs) == 1:
        p, n = outputs[0]
        return p, n, 'application/pdf', os.path.getsize(p)
    zip_path, zip_name, size = _zip_outputs(out_dir, outputs, 'pdfs-compressed.zip')
    return zip_path, zip_name, 'application/zip', size


def run_image_to_pdf(params, in_dir, out_dir, progress):
    from PIL import Image

    files = _list_inputs(in_dir, ALLOWED_IMG)
    if not files:
        raise ValueError('No valid images provided')

    pil_images = []
    for _, path in files:
        pil_images.append(Image.open(path).convert("RGB"))

    out_name = params.get('outname') or 'converted.pdf'
    if not out_name.lower().endswith('.pdf'):
        out_name = out_name + '.pdf'
    out_path = _stage_to_out_path(out_dir, out_name)
    pil_images[0].save(out_path, save_all=True, append_images=pil_images[1:], format="PDF")
    if progress:
        progress(0.95)
    return out_path, out_name, 'application/pdf', os.path.getsize(out_path)


def run_combine_pdfs(params, in_dir, out_dir, progress):
    from PyPDF2 import PdfReader, PdfWriter

    files = _list_inputs(in_dir, ALLOWED_PDF)
    if not files:
        raise ValueError('No valid PDFs to combine')

    writer = PdfWriter()
    total = len(files)
    for i, (_, path) in enumerate(files, 1):
        reader = PdfReader(path)
        for page in reader.pages:
            writer.add_page(page)
        if progress and total:
            progress((i / total) * 0.9)

    out_name = (params.get('outname') or '').strip() or 'combined.pdf'
    if not out_name.lower().endswith('.pdf'):
        out_name = out_name + '.pdf'
    out_path = _stage_to_out_path(out_dir, out_name)
    with open(out_path, 'wb') as f:
        writer.write(f)
    if progress:
        progress(0.95)
    return out_path, out_name, 'application/pdf', os.path.getsize(out_path)


def run_text_to_pdf(params, in_dir, out_dir, progress):
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import letter

    text = (params.get('text') or '').replace('\r\n', '\n')
    out_name = params.get('outname') or 'text.pdf'
    if not out_name.lower().endswith('.pdf'):
        out_name = out_name + '.pdf'
    out_path = _stage_to_out_path(out_dir, out_name)

    c = canvas.Canvas(out_path, pagesize=letter)
    width, height = letter
    x, y = 50, height - 50
    line_height = 14

    for line in text.split('\n'):
        while len(line) > 100:
            c.drawString(x, y, line[:100])
            line = line[100:]
            y -= line_height
            if y < 50:
                c.showPage()
                y = height - 50
        c.drawString(x, y, line)
        y -= line_height
        if y < 50:
            c.showPage()
            y = height - 50
    c.save()
    if progress:
        progress(0.95)
    return out_path, out_name, 'application/pdf', os.path.getsize(out_path)


def run_word_to_pdf(params, in_dir, out_dir, progress):
    from docx import Document
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import letter

    files = _list_inputs(in_dir, ALLOWED_DOC)
    if not files:
        raise ValueError('No Word file uploaded')
    if len(files) > 1:
        raise ValueError('Only one Word file at a time')
    name, path = files[0]

    out_name = f"{name.rsplit('.', 1)[0]}.pdf"
    out_path = _stage_to_out_path(out_dir, out_name)

    doc = Document(path)
    c = canvas.Canvas(out_path, pagesize=letter)
    width, height = letter
    x, y = 50, height - 50
    line_height = 14

    paragraphs = list(doc.paragraphs)
    total = max(len(paragraphs), 1)
    for i, para in enumerate(paragraphs, 1):
        text = para.text or ""
        while len(text) > 100:
            c.drawString(x, y, text[:100])
            text = text[100:]
            y -= line_height
            if y < 50:
                c.showPage()
                y = height - 50
        c.drawString(x, y, text)
        y -= line_height
        if y < 50:
            c.showPage()
            y = height - 50
        if progress and total:
            progress((i / total) * 0.95)
    c.save()
    return out_path, out_name, 'application/pdf', os.path.getsize(out_path)


def run_pdf_to_word(params, in_dir, out_dir, progress):
    from pdf2docx import Converter

    files = _list_inputs(in_dir, ALLOWED_PDF)
    if not files:
        raise ValueError('No PDF uploaded')

    outputs = []
    total = len(files)
    for i, (name, path) in enumerate(files, 1):
        out_name = f"{name.rsplit('.', 1)[0]}.docx"
        out_path = _stage_to_out_path(out_dir, out_name)
        try:
            cv = Converter(path)
            cv.convert(out_path, start=0, end=None)
            cv.close()
        except Exception:
            log.exception('pdf_to_word failed for %s', name)
            try:
                os.remove(out_path)
            except Exception:
                pass
            continue
        outputs.append((out_path, out_name))
        if progress and total:
            progress((i / total) * 0.95)

    if not outputs:
        raise ValueError('No PDFs converted')
    if len(outputs) == 1:
        p, n = outputs[0]
        return p, n, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', os.path.getsize(p)
    zip_path, zip_name, size = _zip_outputs(out_dir, outputs, 'pdfs-to-docx.zip')
    return zip_path, zip_name, 'application/zip', size

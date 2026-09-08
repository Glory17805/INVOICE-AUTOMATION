# The API, with everything the offline reader needs to read a scan.
#
# A container rather than App Service's built-in Python runtime because OCR
# needs Tesseract, which is a system package and not something a requirements
# file can install.
FROM python:3.12-slim

# tesseract-ocr        the OCR engine pytesseract wraps
# tesseract-ocr-eng    the English language data, a separate package on Debian
# libgl1, libglib2.0-0  pillow's runtime dependencies for image handling
#
# poppler-utils is deliberately absent: ocr.py reads scanned pages through the
# images pypdf can already reach, and only falls back to pdf2image for the
# unusual file whose pages are drawn rather than placed. Add it here if one of
# those turns up.
RUN apt-get update \
 && apt-get install --no-install-recommends -y \
      tesseract-ocr \
      tesseract-ocr-eng \
      libgl1 \
      libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Requirements first, so a code change does not reinstall the dependency tree
# - which includes the ~400MB of OCR packages.
COPY requirements.txt requirements-ocr.txt ./
RUN python -m pip install --upgrade pip \
 && python -m pip install -r requirements-ocr.txt

COPY app ./app
COPY tools ./tools
COPY templates ./templates

# The data directory is a mount point in the deployment (Azure Files) and a
# plain directory when run locally. Created either way so a first run without a
# mount still starts rather than failing on a missing path.
ENV GST_DATA_DIR=/data
RUN mkdir -p /data

# Runs as a non-root user, which also means /data must be writable by it. The
# uid is fixed rather than left to the base image so the Azure Files mount can
# be given the same one.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin gst \
 && chown -R gst:gst /app /data
USER gst

# App Service tells the container which port to listen on. Defaulting to 8000
# keeps `docker run -p 8000:8000` working unchanged.
ENV PORT=8000
EXPOSE 8000

# One worker, always. workbook.py serialises writes with an in-process lock,
# which is worth nothing across processes: two workers would both append a row
# at what each believed was the first free line, and the second save would win.
# singleton.py refuses to start a second process for the same reason, so a
# second worker here would not silently corrupt - it would crash-loop. Either
# way the number is 1 and not a tuning knob.
CMD exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT}" --workers 1

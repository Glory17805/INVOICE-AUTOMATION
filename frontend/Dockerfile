# The static frontend.
#
# Its own image, and its own App Service app, so the two halves restart and
# fail independently - which is the reason they were split in the first place.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# server.py is Python standard library only - no requirements to install. The
# file is copied anyway so that adding a dependency later does not silently
# fail to be installed.
COPY requirements.txt ./
RUN if [ -s requirements.txt ] && grep -qv '^\s*\(#.*\)\?$' requirements.txt; then \
      python -m pip install --no-cache-dir -r requirements.txt; \
    fi

COPY server.py ./
COPY public ./public

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin gst \
 && chown -R gst:gst /app
USER gst

ENV PORT=3000
EXPOSE 3000

# --host 0.0.0.0 is required, not tidiness. server.py defaults to 127.0.0.1,
# which is the right default for a laptop and useless in a container: it binds
# the container's own loopback, so traffic arriving on eth0 from the port
# forward is refused. The symptom is a connection that opens and then returns
# an empty reply, which reads like a crashed app rather than a bind address.
#
# GST_API_BASE is what server.py bakes into /config.js and into the page's
# connect-src, so the browser is told where the API is at container start
# rather than at build time. Pointing this deployment at a different API is a
# restart, not a rebuild.
CMD exec python server.py --host 0.0.0.0 --port "${PORT}" --api "${GST_API_BASE}"

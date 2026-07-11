# The service wrapper rides ON the shared bots image: torch, ffmpeg, yt-dlp,
# minion_core and minions are already baked in, so EVERY Step works as a
# service -- we add only the web layer. Published by
# .github/workflows/image.yml as ghcr.io/coppermilk/minion2-service, so the
# NAS pulls it (it never compiles torch). One image, N services: STEP selects
# the Step, SKIN the facade (http | mcp). Bytes in, bytes out -- no object
# store, so a service is stateless and needs no volume.
#
# Local build (from the repo root):
#   docker build -t minion-service -f services/Dockerfile .
#   docker run -e STEP=frames -e SKIN=http -p 8082:8000 minion-service
FROM ghcr.io/coppermilk/minion2:latest

# Only the web layer on top of the shared image (pure-python wheels, seconds).
RUN pip install --no-cache-dir \
    'fastapi>=0.115' 'uvicorn>=0.30' 'pydantic>=2.7' 'mcp>=1.2'

# The base image already has WORKDIR /app with minion_core + minions installed.
COPY services ./services

# Defaults are the offline/dev shape; compose overrides per service.
ENV STEP=deliver SKIN=http PORT=8000
EXPOSE 8000
CMD ["python", "-m", "services.serve"]

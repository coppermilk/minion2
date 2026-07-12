# The service image rides ON the shared bots image: torch, ffmpeg, yt-dlp,
# minion_core and minions are already baked in -- we add only the web layer.
# Published by .github/workflows/image.yml as ghcr.io/coppermilk/minion2-service,
# so the NAS pulls it (it never compiles torch). One image, N services: each
# container runs its own minion (python -m minions.<name>.service), which
# imports ONLY its Step. SKIN picks the facade (http | mcp). Bytes in, bytes
# out -- stateless, no volume.
#
# Local build + run one service (from the repo root):
#   docker build -t minion-service -f services/Dockerfile .
#   docker run -e SKIN=http -p 8091:8000 minion-service \
#     python -m minions.censor_blur.service
FROM ghcr.io/coppermilk/minion2:latest

# Only the web layer on top of the shared image (pure-python wheels, seconds).
RUN pip install --no-cache-dir \
    'fastapi>=0.115' 'uvicorn>=0.30' 'pydantic>=2.7' 'mcp>=1.2'

# The base image already has WORKDIR /app with minion_core + minions installed.
COPY services ./services

# Compose sets the command per service; this default just prints how to run.
ENV SKIN=http PORT=8000
EXPOSE 8000
CMD ["python", "-c", "print('run: python -m minions.<name>.service')"]

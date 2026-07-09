# One image, N services (PLATFORM.md, section 3): STEP selects the Step,
# SKIN the facade (http | mcp). Build from the REPO ROOT so the kernel and
# the catalog are in the context:
#   docker build -t minion-service -f services/Dockerfile .
# Run one service per container, each on its own port:
#   docker run -e STEP=frames -e SKIN=http -p 8081:8000 minion-service
FROM python:3.12-slim
WORKDIR /app

# The kernel + Steps (the IP) install as the bananaland package; the
# service tier rides alongside on the working dir.
COPY pyproject.toml README.md ./
COPY minion_core ./minion_core
COPY minions ./minions
RUN pip install --no-cache-dir .

COPY services ./services
RUN pip install --no-cache-dir \
    'fastapi>=0.115' 'uvicorn>=0.30' 'pydantic>=2.7' 'boto3>=1.34' 'mcp>=1.2'

# Defaults are the offline/dev shape; compose overrides per service.
ENV STEP=deliver SKIN=http PORT=8000 STORE_ROOT=/data/store
EXPOSE 8000
CMD ["python", "-m", "services.serve"]

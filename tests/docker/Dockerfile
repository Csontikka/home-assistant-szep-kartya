# Test runner: Home Assistant 2026.9.2 test harness on Python 3.14.
# docker build -t szep-kartya-test --label project=szep-kartya tests/docker
FROM python:3.14-slim
LABEL project=szep-kartya
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential git libturbojpeg0 \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir pytest-homeassistant-custom-component==0.13.365
WORKDIR /src
CMD ["pytest", "-q"]

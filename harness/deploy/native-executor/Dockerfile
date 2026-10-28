FROM python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3
LABEL org.opencontainers.image.title="Rocoto isolated native Codex staging proof"
LABEL org.opencontainers.image.version="codex-0.153.4"
LABEL com.rocoto.codex.sha256="56ef98ab4032d317ab26e9b5e5a175650717351edb16ed9cde0cb6d1734d62da"
LABEL com.rocoto.codex-code-mode-host.sha256="3e85d67471825f73d02ff5f7e047ca1f6ca8caa3f59e4c6e8d9ca6ca7302cb45"
LABEL com.rocoto.ripgrep.version="15.2.0"
LABEL com.rocoto.ripgrep.sha256="e62198eb19b136b88c330af83647b5a962cb99b6b1f066758568f12de1974849"
COPY codex /usr/local/bin/codex
COPY codex-code-mode-host /usr/local/bin/codex-code-mode-host
COPY rg /usr/local/bin/rg
COPY licenses/ripgrep /usr/local/share/licenses/ripgrep
COPY relay.py /usr/local/lib/rocoto/relay.py
COPY entrypoint.py /usr/local/lib/rocoto/entrypoint.py
COPY probe.py /usr/local/lib/rocoto/probe.py
COPY tooling_probe.py /usr/local/lib/rocoto/tooling_probe.py
RUN mkdir -p /workspace /codex-state /home/rocoto /run/rocoto && chown 10001:10001 /workspace /codex-state /home/rocoto && chmod 0755 /usr/local/bin/codex /usr/local/bin/codex-code-mode-host /usr/local/bin/rg
ENV HOME=/home/rocoto CODEX_HOME=/codex-state PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
WORKDIR /workspace
ENTRYPOINT ["python3", "/usr/local/lib/rocoto/entrypoint.py"]

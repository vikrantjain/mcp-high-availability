FROM python:3.12-slim

WORKDIR /app

RUN pip install uv

COPY pyproject.toml .python-version ./
RUN uv sync --no-dev

COPY server.py .

EXPOSE 8000

CMD ["uv", "run", "fastmcp", "run", "./server.py", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000"]

# Render (Docker Runtime) 向け。ffmpeg、日本語フォント、Chromium(Playwright)を含める。
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY . .

# ブラウザから動画生成を実行できるWebページ(web/app.py)が起動する。
# PORT はRenderが自動で渡す(未設定時は8080)。
CMD ["python3", "web/app.py"]

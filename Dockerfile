# Базовый образ с Python (slim для меньшего размера)
FROM python:3.12-slim

# Устанавливаем системные зависимости для Chrome
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    unzip \
    ca-certificates \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libc6 \
    libcairo2 \
    libcups2 \
    libdbus-1-3 \
    libexpat1 \
    libfontconfig1 \
    libgcc1 \
    libgdk-pixbuf2.0-0 \
    libglib2.0-0 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libstdc++6 \
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxcomposite1 \
    libxcursor1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxi6 \
    libxrandr2 \
    libxrender1 \
    libxtst6 \
    lsb-release \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/*

# Устанавливаем Google Chrome
RUN wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | apt-key add - \
    && echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

# Устанавливаем chromedriver (автоматически под версию Chrome)
RUN CHROME_VERSION=$(google-chrome --version | grep -oP '\d+\.\d+\.\d+') \
    && CHROMEDRIVER_VERSION=$(wget -qO- https://chromedriver.storage.googleapis.com/LATEST_RELEASE_${CHROME_VERSION%.*}) \
    && wget -O /tmp/chromedriver.zip https://chromedriver.storage.googleapis.com/${CHROMEDRIVER_VERSION}/chromedriver_linux64.zip \
    && unzip /tmp/chromedriver.zip -d /usr/local/bin/ \
    && chmod +x /usr/local/bin/chromedriver \
    && rm /tmp/chromedriver.zip

# Рабочая директория
WORKDIR /app

# Копируем файлы
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Переменные окружения (можно переопределить в панели Timeweb)
ENV PORT=8000

# Запуск (предполагаю, что основной файл bot.py; если main.py — поправьте)
CMD uvicorn bot:app --host 0.0.0.0 --port $PORT
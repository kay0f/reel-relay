# reel-relay на NAS (форк kay0f)

Что добавлено в форке относительно оригинала:

| Что | Зачем |
|---|---|
| `YTDLP_COOKIES_FILE` | Cookies из файла `cookies.txt` (на NAS нет браузера). Оригинал монтируется read-only, рабочая копия — в `/data/cookies`, обновляется при новом экспорте |
| `STT_*` | Транскрипция через любой OpenAI-совместимый Whisper. По умолчанию NanoGPT `Whisper-Large-V3`; если verbose_json не поддерживается — откат на json |
| Карусели `/p/` | gallery-dl качает все слайды; картинки становятся кадрами «slide N», видео-слайды транскрибируются |
| Подпись автора | Хранится и показывается в `get_reel` (`Author's caption`) |
| `add_reel(url)` | MCP-инструмент: ссылка прямо в чате → скачать, обработать, вернуть результат. Только `instagram.com`, ссылки нормализуются (без `?igsh=`), повторная ссылка не качается заново |
| Субтитры `ru` | yt-dlp берёт и русские субтитры, если они есть |
| Docker | `Dockerfile`, `deploy/compose.nas.yml`, `deploy/.env.nas.example` |

## Развёртывание

Все команды — на хосте NAS (не через mcp-sysadmin: пересоздание контейнеров убивает его самого).

```bash
# 1. Код
mkdir -p /volume1/docker/reel-relay/{data,secrets}
git clone https://github.com/kay0f/reel-relay /volume1/docker/reel-relay/src

# 2. Секреты
cp /volume1/docker/reel-relay/src/deploy/.env.nas.example /volume1/docker/reel-relay/.env
chmod 600 /volume1/docker/reel-relay/.env
# заполнить INGEST_SECRET, MCP_PATH_TOKEN (python3 -c "import secrets; print(secrets.token_urlsafe(32))"),
# STT_API_KEY (ключ NanoGPT)

# 3. Cookies технического аккаунта (экспорт Netscape из "Get cookies.txt LOCALLY")
#    положить файлом: /volume1/docker/reel-relay/secrets/instagram_cookies.txt
chmod 600 /volume1/docker/reel-relay/secrets/instagram_cookies.txt
# контейнер работает от uid 10001: дать ему право читать
chown -R 10001:10001 /volume1/docker/reel-relay/data /volume1/docker/reel-relay/secrets

# 4. Запуск (в сети librechat_default, без публикации порта наружу)
cd /volume1/docker/reel-relay/src
docker compose -f deploy/compose.nas.yml up -d --build
docker logs -f reel-relay        # ждём "MCP endpoint ready"
```

## Проверки

```bash
# здоровье
docker exec reel-relay python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8787/health').read())"

# выходной IP контейнера: если это IP VPN-датацентра, Instagram может резать
docker exec reel-relay python -c "import urllib.request;print(urllib.request.urlopen('https://api.ipify.org').read())"

# тестовая загрузка (рилс)
docker exec reel-relay sh -c 'python - <<EOF
import json, os, urllib.request
req = urllib.request.Request("http://127.0.0.1:8787/ingest",
    data=json.dumps({"url": "https://www.instagram.com/reel/DepgeJetCaW/"}).encode(),
    headers={"X-Ingest-Secret": os.environ["INGEST_SECRET"], "Content-Type": "application/json"})
print(urllib.request.urlopen(req).read())
EOF'
docker logs --tail 20 reel-relay   # ждём "[id] ready: N frames, M transcript segments"
```

## Подключение к Open WebUI

Admin → Settings → Integrations → External Tools → добавить MCP (Streamable HTTP):

- URL: `http://reel-relay:8787/mcp-<MCP_PATH_TOKEN>/mcp`
- **ID задать явно** (например `reel-relay`) — пустое поле превращается в `auto`
- Доступ: **Публичное** — иначе агенты инструмент не видят
- Включить у нужных моделей (SMM, Аналитик)

Проверка в чате: «разбери https://www.instagram.com/reel/DepgeJetCaW/» → модель вызывает `add_reel`.

Важно проверить: передаёт ли Open WebUI модели **картинки** из результата MCP-инструмента. Если модель видит только текст (транскрипт и подпись), кадры не доходят — тогда для разбора визуала нужен режим, где сервер сам описывает кадры vision-моделью. Решаем по результату теста.

## Подключение к claude.ai (позже)

Нужен постоянный HTTPS-адрес (Tailscale Funnel или именованный Cloudflare Tunnel). Быстрый `cloudflared --url` не подходит: адрес меняется при каждом перезапуске. URL коннектора: `https://<хост>/mcp-<MCP_PATH_TOKEN>/mcp`. Этот URL — единственный пароль, нигде не публиковать.

## Обслуживание

- Cookies протухли (ошибки «login required» в `get_reel`) → заново экспортировать и положить файл, перезапуск не нужен: рабочая копия обновится при следующей загрузке.
- Обновление кода: `git -C /volume1/docker/reel-relay/src pull && docker compose -f deploy/compose.nas.yml up -d --build`.
- Аккаунт забанили → новый технический аккаунт, новый cookies.txt; основной аккаунт не затронут.

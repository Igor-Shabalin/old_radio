# 📻 RadioBook

**Проигрыватель аудиокниг и интернет-радио** в корпусе советской радиоточки «Малютка» (артель «Пластмасс», Красное Село, 1950-е).

Audiobook player + internet radio in a vintage Soviet bakelite radio enclosure.

---

## Что это

RadioBook — автономный проигрыватель аудиокниг и интернет-радио. Управляется одной ручкой громкости и через веб-интерфейс в браузере. Книги играют подряд в заданном порядке. Позиция сохраняется. Интернет не нужен.

Повернул ручку — играет. Повернул обратно — тишина. Как радио, которое всегда было радио.

## Возможности

**Аудиокниги:**
- Загрузка книг через браузер
- Drag & drop сортировка на виртуальной полке
- Автоматическое воспроизведение книг подряд по порядку на полке
- Сохранение позиции для каждой книги
- Работает без интернета

**Интернет-радио:**
- Более 50 000 станций со всего мира
- Офлайн-каталог для мгновенного поиска
- Preview станций перед добавлением
- Добавление станций по URL

**Управление:**
- Ручка громкости = выключатель: повернул — продолжает с того же места
- Веб-интерфейс для настройки (нужен редко)
- Автовозобновление при включении усилителя

## Железо

- **Корпус:** радиоточка «Малютка», бакелит, арт-деко, 1950-е
- **Компьютер:** Banana Pi M2 Zero (Allwinner H3, 512 МБ RAM)
- **Динамик:** 4Ω 3W, моно
- **Усилитель:** класс D (PAM8403), 5V
- **Связь с усилителем:** оптопара PC817 на GPIO
- **Память:** MicroSD 32 ГБ (система + 20–30 аудиокниг)
- **Питание:** USB Type-C, 5V

## Схема подключения оптопары

```
Усилитель 5В ──[ 330Ω ]──► Анод (pin 1) PC817
                            Катод (pin 2) ──► GND усилителя

Pi 3.3В ──[ 10кΩ ]──┬──► GPIO pin 1 (BCM)
                     │
              Коллектор (pin 4) PC817
              Эмиттер  (pin 3) ──► GND Pi

⚡ Земли Pi и усилителя НЕ соединять!
```

Логика инвертирована: GPIO=0 → усилитель ВКЛ, GPIO=1 → ВЫКЛ.

## Установка

```bash
sudo apt update
sudo apt install mpv python3-pip python3-venv ffmpeg

git clone https://github.com/USERNAME/radiobook.git
cd radiobook
python3 -m venv .venv
.venv/bin/pip install flask requests

mkdir -p books tmp
```

## Запуск

```bash
# Без GPIO (для теста на обычном ПК)
.venv/bin/python3 server.py

# С GPIO (на Pi, с оптопарой на пине 1)
sudo RADIOBOOK_GPIO=true RADIOBOOK_GPIO_PIN=1 .venv/bin/python3 server.py
```

Открой в браузере: `http://<IP>:8080`

## Автозапуск (systemd)

```bash
sudo nano /etc/systemd/system/radiobook.service
```

```ini
[Unit]
Description=RadioBook
After=network.target sound.target

[Service]
Type=simple
User=root
WorkingDirectory=/home/pi/radiobook
Environment=RADIOBOOK_GPIO=true
Environment=RADIOBOOK_GPIO_PIN=1
ExecStart=/home/pi/radiobook/.venv/bin/python3 /home/pi/radiobook/server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable radiobook
sudo systemctl start radiobook
```

## Каталог радиостанций

Для офлайн-поиска скачай каталог (~30 МБ, ~54 000 станций):

```bash
curl -o catalog.csv 'https://de2.api.radio-browser.info/csv/stations/search?hidebroken=false&limit=100000'
```

Или нажми «⬇ Обновить» в интерфейсе.

## Конфигурация

| Переменная | По умолчанию | Описание |
|---|---|---|
| `RADIOBOOK_HOME` | `/home/pi/radiobook` | Корневая папка |
| `RADIOBOOK_PORT` | `8080` | Порт |
| `RADIOBOOK_GPIO` | `false` | GPIO-мониторинг усилителя |
| `RADIOBOOK_GPIO_PIN` | `1` | Номер GPIO пина |
| `RADIOBOOK_AMP_ACTIVE_LOW` | `true` | Инвертированная логика (оптопара) |
| `RADIOBOOK_PROXY` | — | HTTP-прокси для API radio-browser |
| `RADIOBOOK_STREAM_PROXY` | `http://127.0.0.1:3128` | Прокси для аудиопотоков |

## Структура проекта

```
radiobook/
├── server.py          # Flask API
├── player.py          # mpv IPC wrapper
├── amp_monitor.py     # GPIO-мониторинг усилителя
├── config.py          # Конфигурация
├── static/
│   └── index.html     # Весь UI
├── books/             # Аудиокниги (по папкам)
├── state.json         # Состояние (позиции, станции, порядок полки)
└── catalog.csv        # Офлайн-каталог радиостанций
```

## API

```
GET  /api/books                    # Список книг (в порядке полки)
POST /api/books                    # Загрузка книги (multipart)
POST /api/books/reorder            # Изменить порядок на полке
POST /api/select/<id>              # Выбрать книгу
POST /api/play                     # Играть
POST /api/pause                    # Пауза
POST /api/next_file                # Следующий файл
POST /api/prev_file                # Предыдущий файл

GET  /api/radio/stations           # Список станций
GET  /api/radio/search?name=...    # Поиск (офлайн-first)
POST /api/radio/play/<sid>         # Играть станцию

GET  /api/status                   # Текущее состояние
GET  /api/radio/diag               # Диагностика сети
GET  /api/radio/catalog/diag       # Диагностика каталога
```

Полный список — в [BRIEFING.md](BRIEFING.md).

## Совместимость

Разработано и протестировано на Banana Pi M2 Zero (Armbian/Debian). Работает на любом Linux-одноплатнике: Raspberry Pi Zero/3/4/5, Orange Pi и др. Для теста можно запустить на обычном ПК с Linux/macOS (без GPIO).

## Лицензия

MIT — см. [LICENSE](LICENSE).

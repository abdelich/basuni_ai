# Что заполнить для запуска BasuniAI

## 1. Файл `.env` (обязательно)

Создай `.env` в корне проекта (скопируй из `.env.example`). Заполни:

| Переменная | Нужно для | Обязательно |
|------------|-----------|-------------|
| `DISCORD_TOKEN_ELDER` | Бот «Старейшина» | **Да** (при `enabled_roles: [elder]`) |
| `OPENAI_API_KEY` | Ответы агента (GPT) | **Да** |
| `DATABASE_URL` | БД (дела, сообщения) | Нет — по умолчанию SQLite в `data/basuni.db` |
| `OPENAI_MODEL` | Модель GPT | Нет — по умолчанию `gpt-4o-mini` |
| `DISCORD_TOKEN_COUNCIL`, `DISCORD_TOKEN_PROSECUTOR`, … | Другие боты | Только если добавишь их в `enabled_roles` в YAML |

**Важно:** В `.env.example` не храни реальные ключи — только подставь свои значения в локальный `.env` (он в `.gitignore`).

---

## 2. Файл `config/default.yaml`

У тебя уже заполнены:

- `guild_id` — ID сервера Discord  
- `channels` — ID каналов (elder_inbox, elder_decisions, court_inbox, council_inbox, …)  
- `role_ids` — ID ролей (pmj, elder, council, prosecutor, judge, …)  
- `reference_category_name` — категория с законом (например «📜 право»)  
- `enabled_roles: [elder]`  
- `roles.elder` — привязка каналов старейшин  

**Проверь:**

- **ID гильдии** — совпадает с твоим сервером (ПКМ по названию сервера → «Копировать ID сервера»).
- **ID каналов** — каждый канал: ПКМ → «Копировать ID канала». Должны совпадать ключи в `channels:` и ключи в `roles.elder.*_channel_key`.
- **ID ролей** — ПКМ по роли в «Управление сервером» → «Копировать ID роли». Роль `pmj` обязательна для проверки «только с ПМЖ».
- **Категория «право»** — точное название категории в Discord, в которой лежат каналы с законом/прецедентами (с эмодзи или без).

Если подключаешь других ботов (council, prosecutor, judge, kpp):

- Добавь их в `enabled_roles`.
- Добавь в `.env` соответствующие токены: `DISCORD_TOKEN_COUNCIL`, `DISCORD_TOKEN_PROSECUTOR` и т.д.
- В `config/default.yaml` добавь блоки `roles.council`, `roles.prosecutor` и т.д. по аналогии с `roles.elder` (inbox, decisions, notify_* и т.п.).

---

## 3. Discord Developer Portal

- Создай приложение (или используй существующее).
- В разделе **Bot** создай бота и скопируй **Token** → это `DISCORD_TOKEN_ELDER`.
- Включи **Privileged Gateway Intents**:  
  - **Server Members Intent** — чтобы бот видел участников и их роли (иначе «роли автора» могут быть пустыми).  
- Пригласи бота на сервер по OAuth2 URL с правами: Read Message History, Send Messages, View Channels, Read Messages.

---

## 4. Итог: минимум для работы старейшины

1. **`.env`**: `DISCORD_TOKEN_ELDER=...`, `OPENAI_API_KEY=...`
2. **`config/default.yaml`**: уже заполнен под твой сервер (guild_id, channels, role_ids, reference_category_name, roles.elder).
3. **Discord**: Server Members Intent включён, бот на сервере, права на чтение/отправку в нужных каналах.

После этого запуск `python main.py` (или через оркестратор) должен поднимать бота «Старейшина» и он будет читать закон из категории «право», принимать обращения и уведомлять суд/совет.
